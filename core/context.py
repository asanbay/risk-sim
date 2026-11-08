"""
core/context.py — единая точка сборки контекста симуляции.

Зачем: правила, Monte Carlo, агенты, экспорт в Blender и отчёт обязаны видеть
ОДИНАКОВЫЕ настройки (плотность воздуха, профиль ветра, DAF, лимиты). Если
каждый модуль собирает их сам, расхождения появятся незаметно. Здесь они
собираются один раз и передаются дальше.

Context также собирает предупреждения о качестве входных данных (data
quality warnings) — они печатаются в отчёт, потому что «мусор на входе»
должен быть виден, а не спрятан.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from core.config import RulesConfig, load_rules
from core.io.loaders import Dataset, load_dataset
from core.physics.wind import WindField, wind_field_from_series
from core.sim.lifts import LiftAssessment, assess_all_lifts


@dataclass
class Context:
    cfg: RulesConfig
    ds: Dataset
    wind: WindField
    data_dir: Path
    warnings: list[str] = field(default_factory=list)
    lift_assessments: list[LiftAssessment] = field(default_factory=list)

    # ------------------------------------------------------------------
    def build_lift_assessments(self, *, run_swing: bool = True, engine: str = "analytic",
                               step_min: float = 15.0) -> list[LiftAssessment]:
        if not self.lift_assessments:
            self.lift_assessments = assess_all_lifts(
                self.ds, self.wind, self.cfg, run_swing=run_swing,
                swing_engine=engine, step_min=step_min)
        return self.lift_assessments

    def daf(self, mode: str = "normal") -> float:
        tbl = self.cfg.physics.get("dynamic_amplification", {}) or {}
        return float(tbl.get(mode, tbl.get("normal", 1.2)))

    def max_work_height_m(self) -> float:
        return max([z.z_top for z in self.ds.zones] + [l.hook_height_m for l in self.ds.lifts] + [0.0])

    def info(self) -> dict[str, Any]:
        from risk_sim.model import load_model_metadata
        span = self.ds.time_span
        return {
            "data_dir": str(self.data_dir),
            "rules_version": self.cfg.meta.get("version"),
            "project": self.cfg.meta.get("project"),
            "zones": len(self.ds.zones),
            "tasks": len(self.ds.tasks),
            "lifts": len(self.ds.lifts),
            "crews": len(self.ds.crews),
            "schedule_span": [span[0].strftime("%Y-%m-%d %H:%M"), span[1].strftime("%Y-%m-%d %H:%M")],
            "max_work_height_m": round(self.max_work_height_m(), 1),
            "ifc_model": load_model_metadata(self.data_dir),
            "wind": self.wind.info(),
            "warnings": self.warnings,
        }


def build_context(data_dir: str | Path = "data/sample", rules_name: str = "rules.yaml",
                  *, run_swing: bool = False, swing_engine: str = "analytic") -> Context:
    """Собирает Context: правила + данные + поле ветра + предупреждения."""
    root = Path(data_dir)
    cfg = load_rules(root / rules_name)
    ds = load_dataset(root)

    wf = wind_field_from_series(
        ds.wind,
        alpha=float(cfg.meta.get("wind_terrain_alpha", 0.16)),
        elevation_m=float(cfg.meta.get("site_elevation_m", 0.0)),
        rho_model=str(cfg.physics.get("air_density_model", "barometric")),
        z0_m=float(cfg.meta.get("wind_roughness_z0_m", 0.05)),
        gust_policy=str(cfg.meta.get("wind_gust_policy", "measured")),
    )

    ctx = Context(cfg=cfg, ds=ds, wind=wf, data_dir=root)
    ctx.warnings.extend(_data_quality_warnings(ctx))
    model_metadata = ctx.info()["ifc_model"]
    if model_metadata and model_metadata["failed_element_count"]:
        ctx.warnings.append(
            f"IFC: {model_metadata['source_file']}: не удалось обработать "
            f"{model_metadata['failed_element_count']} элементов; подробности в model_import.json."
        )
    if run_swing:
        ctx.build_lift_assessments(run_swing=True, engine=swing_engine)
    return ctx


def _data_quality_warnings(ctx: Context) -> list[str]:
    """Проверки входных данных, которые обязаны попасть в отчёт."""
    w: list[str] = []
    ds, wf, cfg = ctx.ds, ctx.wind, ctx.cfg

    # 1) экстраполяция ветрового профиля далеко за пределы измерения
    max_ratio = float(cfg.meta.get("wind_extrapolation_max_ratio", 8.0))
    zmax = ctx.max_work_height_m()
    r = wf.extrapolation_ratio(zmax, max_ratio)
    if r is not None:
        w.append(
            f"ВЕТЕР: скорость переносится с {wf.ref_height_m:.0f} м на {zmax:.0f} м "
            f"(отношение {r:.0f} > {max_ratio:.0f}). Степенной профиль на таком интервале "
            f"ненадёжен — для высотного здания нужны данные ветроинженерного отчёта "
            f"(аэродинамическая труба / CFD), а не экстраполяция. Все выводы по ветру "
            f"на высоте являются оценочными.")

    # 2) метео-данные не покрывают график работ
    t0, t1 = ds.time_span
    if not wf.covers(t0) or not wf.covers(t1):
        w.append(f"ВЕТЕР: данные {wf.ts.min()} … {wf.ts.max()} не покрывают весь график работ "
                 f"({t0} … {t1}). За пределами ряда значения будут «залипнуты» на крайних — "
                 f"результат недостоверен.")

    # 3) источник ветра
    src = (wf.source or "").lower()
    if "synthetic" in src or not wf.source:
        w.append(f"ВЕТЕР: источник данных — «{wf.source or 'не указан'}». Для реального объекта "
                 f"замените на данные метеостанции площадки.")

    # 4) нет данных порывов -> политика 'measured' невозможна
    if bool((wf.gust_ms == wf.mean_ms).all()):
        w.append("ВЕТЕР: колонка gust_ms совпадает с mean_ms — данных о порывах нет. "
                 "Правила по ветру будут недооценивать риск. Добавьте порывы или переключите "
                 "meta.wind_gust_policy на 'model'.")

    # 5) Cd задан одним числом
    for lf in ds.lifts:
        if lf.cd_min == lf.cd_max:
            w.append(f"ПОДЪЁМ {lf.lift_id}: Cd задан одним числом ({lf.cd_min}). Для плоских "
                     f"грузов корректнее диапазон — проверьте по справочнику ветровой нагрузки.")

    # 6) оттяжка не назначена
    for lf in ds.lifts:
        if lf.tagline_people <= 0:
            w.append(f"ПОДЪЁМ {lf.lift_id}: оттяжка не назначена (tagline_people=0). "
                     f"Если по ППР удержание груза предусмотрено — укажите число людей.")

    # 7) люди на ярусах без ограждения — проверка полноты данных
    edges_open = [z.zone_id for z in ds.zones if z.type == "edge" and not z.guardrail_installed]
    if edges_open:
        w.append(f"ЗОНЫ: открытые края без ограждения на входе: {', '.join(edges_open)}. "
                 f"Сверьте с фактическим состоянием на дату начала работ.")

    # 8) зоны, на которые ссылается график, но которые не помечены типом work
    for t in ds.tasks:
        z = ds.zone_by_id[t.zone_id]
        if z.type == "crane_hazard" and not ({"crane", "lift"} & set(t.hazard_tags)):
            w.append(f"ГРАФИК: задача {t.task_id} назначена в зону опасности крана {z.zone_id}, "
                     f"но не имеет метки crane/lift — проверьте hazard_tags.")
    return w
