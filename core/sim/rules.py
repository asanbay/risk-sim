"""
core/sim/rules.py — движок правил и поиска конфликтов.

Принцип: правило — это функция, которая возвращает список Finding. Движок
ничего не знает о том, как именно правило устроено, поэтому добавить своё
правило = добавить функцию и строку в rules.yaml (с полем `source`).

Каждый Finding несёт:
  * ссылку на пункт ППР/нормы  (rule.source) — чтобы вывод был защищаемым;
  * интервал времени и координату — чтобы его можно было показать в Blender;
  * метрики (числа, которые привели к срабатыванию) — чтобы его можно было
    оспорить или подтвердить ручным расчётом.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from core.config import Rule, RulesConfig
from core.io.loaders import Dataset, WorkTask, Zone
from core.physics.wind import WindField, profile_factor
from core.sim import geometry as G


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    rule_id: str
    title: str
    severity: str
    source: str
    message: str
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    zone_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    crew_ids: list[str] = field(default_factory=list)
    people_exposed: int = 0
    location: tuple[float, float, float] | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    lift_id: str | None = None

    _counter: list[int] = field(default_factory=lambda: [0], repr=False)

    @property
    def duration_min(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        return (self.end - self.start).total_seconds() / 60.0

    @property
    def severity_rank(self) -> int:
        return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(self.severity, 0)

    @property
    def exposure_person_min(self) -> float:
        d = self.duration_min
        return float(self.people_exposed * d) if d else 0.0

    def as_row(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_title": self.title,
            "severity": self.severity,
            "severity_rank": self.severity_rank,
            "source": self.source,
            "message": self.message,
            "start": self.start.strftime("%Y-%m-%d %H:%M") if self.start is not None else "",
            "end": self.end.strftime("%Y-%m-%d %H:%M") if self.end is not None else "",
            "duration_min": round(self.duration_min, 1) if self.duration_min is not None else "",
            "zone_ids": ";".join(self.zone_ids),
            "task_ids": ";".join(self.task_ids),
            "crew_ids": ";".join(self.crew_ids),
            "people_exposed": self.people_exposed,
            "exposure_person_min": round(self.exposure_person_min, 1),
            "x": round(self.location[0], 2) if self.location else "",
            "y": round(self.location[1], 2) if self.location else "",
            "z": round(self.location[2], 2) if self.location else "",
            "lift_id": self.lift_id or "",
            "metrics": "; ".join(f"{k}={v}" for k, v in self.metrics.items()),
        }

    def as_dict(self) -> dict[str, Any]:
        """Машиночитаемая форма. В отличие от as_row() списки остаются
        списками (а не строками через ';') — JSON-потребители не должны
        разбирать склеенные строки обратно."""
        d = self.as_row()
        d["zone_ids"] = list(self.zone_ids)
        d["task_ids"] = list(self.task_ids)
        d["crew_ids"] = list(self.crew_ids)
        d["metrics"] = self.metrics
        d["location"] = list(self.location) if self.location else None
        d["start"] = self.start.strftime("%Y-%m-%d %H:%M") if self.start is not None else None
        d["end"] = self.end.strftime("%Y-%m-%d %H:%M") if self.end is not None else None
        return d


# ---------------------------------------------------------------------------
# движок
# ---------------------------------------------------------------------------
class RuleEngine:
    """Реестр правил. Своё правило добавляется через @RuleEngine.rule('id')."""

    _registry: dict[str, Callable[..., list[Finding]]] = {}

    @classmethod
    def rule(cls, rule_id: str):
        def deco(fn: Callable[..., list[Finding]]):
            cls._registry[rule_id] = fn
            return fn
        return deco

    def __init__(self, ds: Dataset, cfg: RulesConfig, wind: WindField):
        self.ds = ds
        self.cfg = cfg
        self.wind = wind
        self.tasks = sorted(ds.tasks, key=lambda t: t.start)
        self.zones = ds.zones
        self.lift_assessments: list[Any] = []
        self._guardrail_timeline: dict[str, pd.Timestamp] = {}
        self._build_guardrail_timeline()

    # -- вспомогательное -------------------------------------------------
    def _build_guardrail_timeline(self) -> None:
        """Когда в каждой зоне появляется ограждение.

        zones.csv задаёт состояние на начало периода; задача с флагом
        `installs_guardrail` делает зону защищённой с момента своего завершения.
        """
        for z in self.zones:
            if z.type == "edge" and not z.guardrail_installed:
                installs = [t for t in self.tasks
                            if getattr(t, "installs_guardrail", False) and t.zone_id == z.zone_id]
                if installs:
                    self._guardrail_timeline[z.zone_id] = min(t.end for t in installs)

    def guardrail_ok(self, zone_id: str, at: pd.Timestamp) -> bool:
        z = self.ds.zone_by_id.get(zone_id)
        if z is None:
            return True
        if z.guardrail_installed:
            return True
        t_ready = self._guardrail_timeline.get(zone_id)
        return bool(t_ready is not None and at >= t_ready)

    def guardrail_ready_time(self, zone_id: str) -> pd.Timestamp | None:
        return self._guardrail_timeline.get(zone_id)

    def overlapping(self, a: WorkTask, b: WorkTask) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        s, e = max(a.start, b.start), min(a.end, b.end)
        return (s, e) if s < e else None

    def mk(self, rule: Rule, message: str, **kw: Any) -> Finding:
        return Finding(rule_id=rule.id, title=rule.title, severity=rule.severity,
                       source=rule.source, message=message, **kw)

    def people_of(self, tasks: Iterable[WorkTask]) -> int:
        seen: set[str] = set()
        n = 0
        for t in tasks:
            if t.crew_id and t.crew_id not in seen:
                seen.add(t.crew_id)
                n += t.crew_size or 0
        return n

    def wind_at(self, t: pd.Timestamp, z: float, use_gust: bool = True) -> float:
        return self.wind.gust_at(t, z) if use_gust else self.wind.mean_at(t, z)

    def wind_limit_reference_height(self) -> float:
        """Высота, к которой нормирован лимит ветра из ППР/паспорта."""
        return float(self.cfg.meta.get("wind_ref_height_m", 10.0))

    def limit_comparable_wind(self, t: pd.Timestamp, z_work: float, use_gust: bool) -> tuple[float, float, str]:
        """(скорость для сравнения с лимитом, скорость на высоте работ, базис).

        Лимиты паспорта крана нормированы к высоте 10 м. Сравнивать их со
        скоростью на высоте 250 м — методическая ошибка, которая даёт либо
        ложные срабатывания, либо ложное спокойствие. Поэтому:
          basis == 'ref_height'  -> сравниваем измерение на 10 м с лимитом;
          basis == 'work_height' -> пересчитываем лимит на высоту работ.
        В обоих случаях в отчёт идут ОБА числа.
        """
        basis = str(self.cfg.meta.get("wind_limit_basis", "ref_height"))
        z_ref = self.wind_limit_reference_height()
        v_ref = self.wind.gust_at(t, z_ref) if use_gust else self.wind.mean_at(t, z_ref)
        v_work = self.wind.gust_at(t, z_work) if use_gust else self.wind.mean_at(t, z_work)
        return (v_ref if basis == "ref_height" else v_work), v_work, basis

    # -- запуск ----------------------------------------------------------
    def run(self, rule_ids: Sequence[str] | None = None) -> list[Finding]:
        findings: list[Finding] = []
        enabled = {r.id for r in self.cfg.enabled_rules()}
        wanted = list(rule_ids) if rule_ids else [r.id for r in self.cfg.rules]
        unknown = [r for r in wanted if r not in self._registry]
        if unknown:
            print(f"[rules] ВНИМАНИЕ: правила объявлены в rules.yaml, но не реализованы: {unknown}")
        for rid in wanted:
            if rid not in self._registry or rid not in enabled:
                continue
            fn = self._registry[rid]
            findings.extend(fn(self))
        findings.sort(key=lambda f: (-f.severity_rank, f.start or pd.Timestamp.max, f.rule_id))
        return findings

    def run_all(self) -> tuple[list[Finding], pd.DataFrame]:
        f = self.run()
        return f, pd.DataFrame([x.as_row() for x in f])


# ===========================================================================
# ПРАВИЛА
# ===========================================================================
@RuleEngine.rule("no_two_crews_same_zone")
def _r_two_crews(eng: RuleEngine) -> list[Finding]:
    """Две разные бригады в пересекающихся рабочих зонах в одно время."""
    rule = eng.cfg.rule("no_two_crews_same_zone")
    types = set(rule.params.get("zone_types", ["work"]))
    min_min = float(rule.params.get("min_overlap_min", 0))
    out: list[Finding] = []
    work = [t for t in eng.tasks if eng.ds.zone_by_id[t.zone_id].type in types]

    for a, b in itertools.combinations(work, 2):
        if a.crew_id and a.crew_id == b.crew_id:
            continue  # одна бригада — это не совмещение, это её же работа
        za, zb = eng.ds.zone_by_id[a.zone_id], eng.ds.zone_by_id[b.zone_id]
        ov = eng.overlapping(a, b)
        if ov is None:
            continue
        same_zone = a.zone_id == b.zone_id
        area = G.intersection_area(za, zb)
        if not same_zone and area < 1.0:
            continue
        dur_min = (ov[1] - ov[0]).total_seconds() / 60.0
        if dur_min < min_min:
            continue
        c = G.intersection_center(za, zb) or za.center[:2]
        z_mid = max(za.z_floor, zb.z_floor)
        frac = G.overlap_fraction(za, zb)
        out.append(eng.mk(
            rule,
            f"Бригады {a.crew_id} ({a.crew_size} чел.) и {b.crew_id} ({b.crew_size} чел.) работают "
            f"одновременно {'в одной зоне' if same_zone else f'в пересекающихся зонах (перекрытие {frac:.0%} плана)'} "
            f"{a.zone_id}/{b.zone_id} в течение {dur_min/60:.1f} ч.",
            start=ov[0], end=ov[1],
            zone_ids=sorted({a.zone_id, b.zone_id}), task_ids=[a.task_id, b.task_id],
            crew_ids=sorted({a.crew_id, b.crew_id}),
            people_exposed=(a.crew_size or 0) + (b.crew_size or 0),
            location=(c[0], c[1], z_mid),
            metrics={"overlap_m2": round(area, 1), "overlap_fraction": round(frac, 3),
                     "duration_h": round(dur_min / 60.0, 2), "same_zone": same_zone},
        ))
    return out


@RuleEngine.rule("no_work_under_crane_hazard")
def _r_under_crane(eng: RuleEngine) -> list[Finding]:
    """Люди в зоне работы крана во время активного подъёма.

    Режим `dynamic` (по умолчанию) строит зону опасности ОТ ФАКТИЧЕСКОЙ
    ТРАЕКТОРИИ груза: коридор «подъём -> перемещение -> приёмка» плюс круги
    под точками строповки и приёмки. Режим `static` берёт зоны типа
    crane_hazard из zones.csv.

    Почему по умолчанию dynamic: статическая зона «на весь ярус» срабатывает
    на каждую бригаду при каждом подъёме. Правило, которое кричит всегда,
    перестают читать — а это прямая угроза безопасности. Опасность конкретна:
    она там, где в данный момент идёт груз.
    """
    rule = eng.cfg.rule("no_work_under_crane_hazard")
    mode = str(rule.params.get("mode", "dynamic")).lower()
    haz_types = set(rule.params.get("hazard_types", ["crane_hazard"]))
    corridor_w = float(rule.params.get("corridor_width_m", 8.0))
    end_r = float(rule.params.get("end_radius_m", 6.0))
    prox = float(rule.params.get("load_proximity_m", 8.0))
    lvl_tol = float(rule.params.get("same_level_tol_m", 12.0))

    hazards = [z for z in eng.zones if z.type in haz_types]
    people_tasks = [t for t in eng.tasks
                    if eng.ds.zone_by_id[t.zone_id].type in {"work", "passage", "storage"}
                    and (t.crew_size or 0) > 0]
    out: list[Finding] = []

    for lf in eng.ds.lifts:
        ct = eng.ds.task_by_id[lf.task_id]
        poly = None
        if mode in {"dynamic", "both"}:
            poly, _w = G.lift_hazard_polygon(lf, corridor_w, end_r)

        for p in people_tasks:
            if p.crew_id == ct.crew_id:
                continue  # крановая бригада сама выполняет этот подъём
            ov = eng.overlapping(ct, p)
            if ov is None:
                continue
            zp = eng.ds.zone_by_id[p.zone_id]
            near_pick = abs(zp.z_floor - lf.pickup[2]) <= lvl_tol
            near_drop = abs(zp.z_floor - lf.dropoff[2]) <= lvl_tol
            if not (near_pick or near_drop):
                continue  # другой ярус: груз идёт существенно выше/ниже

            area_dyn = float(poly.intersection(G.zone_polygon(zp)).area) if poly is not None else 0.0
            d_path = G.distance_to_polygon(zp.center[0], zp.center[1], poly) if poly is not None else None
            d_pt = min(G.distance_point_to_rect(lf.dropoff[0], lf.dropoff[1], zp),
                       G.distance_point_to_rect(lf.pickup[0], lf.pickup[1], zp))
            hit_static = [h for h in hazards
                          if G.zones_overlap_xy(h, zp) and h.overlaps_z(zp)] if mode in {"static", "both"} else []

            if mode == "dynamic":
                hit, why = area_dyn >= 1.0 or d_pt <= prox, None
            elif mode == "static":
                hit, why = bool(hit_static), None
            else:
                hit, why = (area_dyn >= 1.0 or bool(hit_static) or d_pt <= prox), None
            if not hit:
                continue

            if area_dyn >= 1.0:
                reason = f"траектория груза пересекает зону на {area_dyn:.0f} м²"
            elif d_pt <= prox:
                reason = f"зона в {d_pt:.1f} м от точки строповки/приёмки груза (лимит {prox:.0f} м)"
            else:
                reason = f"зона входит в статическую зону опасности {hit_static[0].zone_id}"
            dur = (ov[1] - ov[0]).total_seconds() / 3600.0

            out.append(eng.mk(
                rule,
                f"Во время подъёма {lf.lift_id} ({lf.load_name}, {lf.mass_kg:.0f} кг) бригада "
                f"{p.crew_id} ({p.crew_size} чел.) находится в зоне {p.zone_id}: {reason}. "
                f"Совпадение по времени {dur:.1f} ч.",
                start=ov[0], end=ov[1], zone_ids=[p.zone_id],
                task_ids=[ct.task_id, p.task_id], crew_ids=[p.crew_id],
                people_exposed=p.crew_size or 0,
                location=(lf.dropoff[0], lf.dropoff[1], lf.dropoff[2]), lift_id=lf.lift_id,
                metrics={"lift_id": lf.lift_id, "mode": mode, "reason": reason,
                         "load_mass_kg": lf.mass_kg,
                         "hazard_area_in_zone_m2": round(area_dyn, 1),
                         "distance_zone_to_path_m": round(d_path, 2) if d_path is not None else None,
                         "distance_to_pickup_dropoff_m": round(d_pt, 2),
                         "proximity_limit_m": prox,
                         "static_hazard_zones": [h.zone_id for h in hit_static],
                         "hours": round(dur, 2), "corridor_width_m": corridor_w},
            ))
    return out


@RuleEngine.rule("edge_work_requires_guardrail")
def _r_edge(eng: RuleEngine) -> list[Finding]:
    """Работа у края перекрытия без ограждения (с учётом графика установки)."""
    rule = eng.cfg.rule("edge_work_requires_guardrail")
    types = set(rule.params.get("zone_types", ["edge"]))
    fall_h = float(rule.params.get("fall_height_m", 1.8))
    out: list[Finding] = []

    for t in eng.tasks:
        if getattr(t, "installs_guardrail", False):
            continue  # монтаж ограждения — сам по себе защищённая операция по своей технологии
        z = eng.ds.zone_by_id[t.zone_id]
        needs = (z.type in types) or t.guardrail_required or ("edge" in t.hazard_tags)
        if not needs:
            continue
        # перепад высот: работа на уровне z_floor над землёй/нижележащим перекрытием
        drop = z.z_floor - 0.0
        if drop < fall_h and z.type in types:
            continue
        if eng.guardrail_ok(t.zone_id, t.start):
            continue
        ready = eng.guardrail_ready_time(t.zone_id)
        msg_extra = ""
        if ready is not None:
            msg_extra = f" Ограждение по графику появится только {ready:%d.%m %H:%M}."
        out.append(eng.mk(
            rule,
            f"Работа '{t.name}' в зоне {t.zone_id} (перепад {drop:.0f} м) начинается "
            f"{t.start:%d.%m %H:%M} без установленного ограждения.{msg_extra}",
            start=t.start, end=t.end, zone_ids=[t.zone_id], task_ids=[t.task_id],
            crew_ids=[t.crew_id], people_exposed=t.crew_size or 0, location=z.center,
            metrics={"fall_height_m": round(drop, 1), "guardrail_installed": False,
                     "guardrail_ready": ready.strftime("%Y-%m-%d %H:%M") if ready is not None else None},
        ))

    # дополнительно: незащищённый край рядом с работающей зоной
    warn_m = float(eng.cfg.people.get("edge_proximity_warn_m", 2.0))
    edges = [z for z in eng.zones if z.type in types]
    for t in eng.tasks:
        if getattr(t, "installs_guardrail", False):
            continue
        zt = eng.ds.zone_by_id[t.zone_id]
        if zt.type in types:
            continue
        for e in edges:
            if eng.guardrail_ok(e.zone_id, t.start):
                continue
            if not (abs(e.z_floor - zt.z_floor) < 0.5):
                continue
            d = _rect_distance(zt, e)
            if d <= warn_m:
                out.append(eng.mk(
                    rule,
                    f"Бригада {t.crew_id} работает в {d:.1f} м от открытого края {e.zone_id} "
                    f"(ограждения нет на {t.start:%d.%m %H:%M}).",
                    start=t.start, end=min(t.end, eng.guardrail_ready_time(e.zone_id) or t.end),
                    zone_ids=[t.zone_id, e.zone_id], task_ids=[t.task_id],
                    crew_ids=[t.crew_id], people_exposed=t.crew_size or 0,
                    location=e.center,
                    metrics={"distance_to_edge_m": round(d, 2), "edge_zone": e.zone_id},
                ))
    return out


def _rect_distance(a: Zone, b: Zone) -> float:
    dx = max(b.x_min - a.x_max, a.x_min - b.x_max, 0.0)
    dy = max(b.y_min - a.y_max, a.y_min - b.y_max, 0.0)
    return math.hypot(dx, dy)


@RuleEngine.rule("stop_crane_ops_above_wind_limit")
def _r_wind(eng: RuleEngine) -> list[Finding]:
    """Ветер выше лимита ППР во время крановых/высотных работ."""
    rule = eng.cfg.rule("stop_crane_ops_above_wind_limit")
    use_gust = bool(rule.params.get("use_gust", True))
    out: list[Finding] = []

    for t in eng.tasks:
        if not math.isfinite(t.wind_limit_ms):
            continue
        if "crane" not in t.hazard_tags and "lift" not in t.hazard_tags and t.equipment_id == "":
            # для некрановых работ лимит ветра тоже может быть задан — проверяем все с лимитом
            pass
        z = eng.ds.zone_by_id[t.zone_id]
        z_work = max(z.z_top, z.z_floor + 2.0)
        hours = pd.date_range(t.start, t.end, freq="h")
        if len(hours) == 0:
            hours = pd.DatetimeIndex([t.start])
        bad: list[tuple[pd.Timestamp, float, float]] = []
        for ts in hours:
            v_cmp, v_work, basis = eng.limit_comparable_wind(ts, z_work, use_gust)
            if v_cmp > t.wind_limit_ms:
                bad.append((ts, v_cmp, v_work))
        if not bad:
            continue
        peak = max(bad, key=lambda x: x[1])
        s = bad[0][0]
        e = min(bad[-1][0] + pd.Timedelta(hours=1), t.end)
        is_crane = ("crane" in t.hazard_tags) or ("lift" in t.hazard_tags) or bool(t.equipment_id)
        exposed = t.crew_size or 0
        out.append(eng.mk(
            rule,
            f"{'Крановая операция' if is_crane else 'Работа'} '{t.name}' при ветре выше лимита ППР "
            f"{t.wind_limit_ms:.0f} м/с: пик {peak[1]:.1f} м/с ({peak[0]:%d.%m %H:%M}), "
            f"на высоте работ {peak[2]:.1f} м/с. Превышение {len(bad)} ч из "
            f"{(t.end-t.start).total_seconds()/3600:.0f} ч окна.",
            start=s, end=e, zone_ids=[t.zone_id], task_ids=[t.task_id], crew_ids=[t.crew_id],
            people_exposed=exposed, location=z.center,
            metrics={"limit_ms": t.wind_limit_ms, "peak_comparable_ms": round(peak[1], 2),
                     "peak_at_work_height_ms": round(peak[2], 2), "hours_over_limit": len(bad),
                     "window_hours": round((t.end - t.start).total_seconds() / 3600.0, 1),
                     "basis": str(eng.cfg.meta.get("wind_limit_basis", "ref_height")),
                     "basis_height_m": eng.wind_limit_reference_height(),
                     "work_height_m": round(z_work, 1), "use_gust": use_gust},
        ))
    return out


@RuleEngine.rule("tagline_force_within_capacity")
def _r_tagline(eng: RuleEngine) -> list[Finding]:
    return _lift_violations(eng, "tagline_force_within_capacity")


@RuleEngine.rule("sling_utilization_limit")
def _r_sling(eng: RuleEngine) -> list[Finding]:
    return _lift_violations(eng, "sling_utilization_limit")


@RuleEngine.rule("crane_radius_within_chart")
def _r_crane_chart(eng: RuleEngine) -> list[Finding]:
    return _lift_violations(eng, "crane_radius_within_chart")


def _lift_violations(eng: RuleEngine, rule_id: str) -> list[Finding]:
    """Преобразует нарушения из LiftAssessment в Finding (единый формат)."""
    rule = eng.cfg.rule(rule_id)
    out: list[Finding] = []
    for la in eng.lift_assessments:
        for v in la.violations:
            if v["rule_id"] != rule_id:
                continue
            z = la.lift.dropoff
            out.append(eng.mk(
                rule, v["message"],
                start=la.window[0], end=la.window[1],
                zone_ids=[], task_ids=[la.task.task_id], crew_ids=[la.task.crew_id],
                people_exposed=la.task.crew_size or 0,
                location=(z[0], z[1], z[2]), lift_id=la.lift.lift_id,
                metrics=dict(v["metric"], lift_id=la.lift.lift_id, load=la.lift.load_name),
            ))
    return out


@RuleEngine.rule("passage_must_stay_clear")
def _r_passage(eng: RuleEngine) -> list[Finding]:
    """Проход/путь эвакуации перекрыт складом или задачей, размещённой в проходе.

    Срабатывает в двух случаях:
      1) задача РАЗМЕЩЕНА в зоне типа passage (склад/материалы в проходе);
      2) задача в зоне storage (или с меткой из blocking_hazard_tags)
         геометрически перекрывает проход на уровне того же яруса.
    Обычные рабочие зоны, которые лишь частично пересекают проход по плану,
    не считаются нарушением: проход остаётся проходимым, если в нём нет
    материалов и техники.
    """
    rule = eng.cfg.rule("passage_must_stay_clear")
    pass_types = set(rule.params.get("zone_types", ["passage"]))
    block_types = set(rule.params.get("blocking_types", ["storage"]))
    block_tags = {str(t).strip().lower() for t in rule.params.get("blocking_hazard_tags", [])}
    min_min = float(rule.params.get("min_overlap_min", 0))
    min_fraction = float(rule.params.get("min_blocked_fraction", 0.10))
    passages = [z for z in eng.zones if z.type in pass_types]
    out: list[Finding] = []

    # 1) задача размещена прямо в зоне прохода
    for t in eng.tasks:
        zt = eng.ds.zone_by_id[t.zone_id]
        if zt.type not in pass_types:
            continue
        if zt.type in block_types or block_tags.intersection({x.lower() for x in t.hazard_tags}):
            dur_h = (t.end - t.start).total_seconds() / 3600.0
            out.append(eng.mk(
                rule,
                f"Задача '{t.name}' размещена в зоне прохода {zt.zone_id} на {dur_h:.1f} ч "
                f"(метки: {', '.join(t.hazard_tags) or '—'}). Путь эвакуации и маршрут носилок перекрыт.",
                start=t.start, end=t.end, zone_ids=[zt.zone_id], task_ids=[t.task_id],
                crew_ids=[t.crew_id], people_exposed=t.crew_size or 0, location=zt.center,
                metrics={"passage_zone": zt.zone_id, "hours": round(dur_h, 2),
                         "blocked_fraction": 1.0},
            ))

    # 2) склад/блокирующая зона перекрывает проход геометрически
    blockers = [t for t in eng.tasks
                if eng.ds.zone_by_id[t.zone_id].type in block_types
                or block_tags.intersection({x.lower() for x in t.hazard_tags})]
    for p in passages:
        for t in blockers:
            zt = eng.ds.zone_by_id[t.zone_id]
            if zt.zone_id == p.zone_id:
                continue
            area = G.intersection_area(p, zt)
            frac = area / max(p.area_m2, 1e-9)
            if frac < min_fraction:
                continue
            if abs(p.z_floor - zt.z_floor) > 0.6:
                continue  # другой ярус
            dur = (t.end - t.start).total_seconds() / 60.0
            if dur < min_min:
                continue
            c = G.intersection_center(p, zt) or p.center[:2]
            out.append(eng.mk(
                rule,
                f"'{t.name}' ({zt.zone_id}) перекрывает {area:.0f} м2 прохода {p.zone_id} "
                f"({frac:.0%} его площади) с {t.start:%d.%m %H:%M} по {t.end:%d.%m %H:%M}.",
                start=t.start, end=t.end, zone_ids=[p.zone_id, zt.zone_id], task_ids=[t.task_id],
                crew_ids=[t.crew_id], people_exposed=t.crew_size or 0,
                location=(c[0], c[1], p.z_floor),
                metrics={"blocked_area_m2": round(area, 1), "blocked_fraction": round(frac, 3)},
            ))
    return out


@RuleEngine.rule("no_simultaneous_lift_and_pour")
def _r_lift_over_pour(eng: RuleEngine) -> list[Finding]:
    """Подъём грузов над зоной бетонирования (подача материалов над людьми)."""
    rule = eng.cfg.rule("no_simultaneous_lift_and_pour")
    pour_tag = str(rule.params.get("pour_tag", "concrete_pour"))
    eq_prefix = str(rule.params.get("pour_equipment_prefix", "CP"))
    pours = [t for t in eng.tasks
             if t.has_tag(pour_tag) or "concrete" in t.hazard_tags
             or t.equipment_id.startswith(eq_prefix)]
    out: list[Finding] = []
    for lf in eng.ds.lifts:
        lt = eng.ds.task_by_id[lf.task_id]
        for p in pours:
            ov = eng.overlapping(lt, p)
            if ov is None:
                continue
            zp = eng.ds.zone_by_id[p.zone_id]
            d = G.distance_point_to_segment(zp.center[0], zp.center[1],
                                            lf.pickup[0], lf.pickup[1], lf.dropoff[0], lf.dropoff[1])
            d_drop = math.hypot(zp.center[0] - lf.dropoff[0], zp.center[1] - lf.dropoff[1])
            if not (d <= 10.0 or zp.contains_xy(lf.dropoff[0], lf.dropoff[1])):
                continue
            dur = (ov[1] - ov[0]).total_seconds() / 3600.0
            out.append(eng.mk(
                rule,
                f"Подъём {lf.lift_id} ({lf.load_name}, {lf.mass_kg:.0f} кг) выполняется над зоной "
                f"бетонирования {zp.zone_id}, где работают {p.crew_size} чел. Совпадение {dur:.1f} ч.",
                start=ov[0], end=ov[1], zone_ids=[p.zone_id], task_ids=[lf.task_id, p.task_id],
                crew_ids=[p.crew_id], people_exposed=p.crew_size or 0,
                location=(lf.dropoff[0], lf.dropoff[1], lf.dropoff[2]), lift_id=lf.lift_id,
                metrics={"distance_path_to_zone_m": round(min(d, d_drop), 2), "hours": round(dur, 2)},
            ))
    return out


@RuleEngine.rule("no_work_below_active_work")
def _r_stacked(eng: RuleEngine) -> list[Finding]:
    """Работы на нижнем ярусе под активной зоной работ верхнего яруса."""
    rule = eng.cfg.rule("no_work_below_active_work")
    gap = float(rule.params.get("min_level_gap_m", 2.5))
    frac_min = float(rule.params.get("min_overlap_fraction", 0.25))
    min_min = float(rule.params.get("min_overlap_min", 30))
    work = [t for t in eng.tasks if eng.ds.zone_by_id[t.zone_id].type == "work"]
    out: list[Finding] = []
    for up, low in itertools.permutations(work, 2):
        zu, zl = eng.ds.zone_by_id[up.zone_id], eng.ds.zone_by_id[low.zone_id]
        if not (zu.z_floor - zl.z_floor >= gap):
            continue
        frac = G.intersection_area(zu, zl) / max(min(zu.area_m2, zl.area_m2), 1e-9)
        if frac < frac_min:
            continue
        ov = eng.overlapping(up, low)
        if ov is None:
            continue
        dur = (ov[1] - ov[0]).total_seconds() / 60.0
        if dur < min_min:
            continue
        if up.crew_id == low.crew_id:
            continue
        c = G.intersection_center(zu, zl) or zl.center[:2]
        out.append(eng.mk(
            rule,
            f"Верхний ярус {zu.zone_id} ({up.task_id}, {up.crew_id}) работает над нижним {zl.zone_id} "
            f"({low.task_id}, {low.crew_id}, {low.crew_size} чел.). Перепад {zu.z_floor-zl.z_floor:.1f} м, "
            f"совпадение {dur/60:.1f} ч, перекрытие в плане {frac:.0%}.",
            start=ov[0], end=ov[1], zone_ids=[zu.zone_id, zl.zone_id],
            task_ids=[up.task_id, low.task_id], crew_ids=sorted({up.crew_id, low.crew_id}),
            people_exposed=low.crew_size or 0, location=(c[0], c[1], zl.z_top),
            metrics={"level_gap_m": round(zu.z_floor - zl.z_floor, 2),
                     "plan_overlap_fraction": round(frac, 3), "hours": round(dur / 60.0, 2)},
        ))
    return out


@RuleEngine.rule("load_path_over_people")
def _r_load_path(eng: RuleEngine) -> list[Finding]:
    """Траектория перемещения груза проходит над зоной, где находятся люди."""
    rule = eng.cfg.rule("load_path_over_people")
    types = set(rule.params.get("consider_zone_types", ["work", "passage"]))
    corridor_w = float(rule.params.get("corridor_width_m", 6.0))
    occupied = [t for t in eng.tasks if eng.ds.zone_by_id[t.zone_id].type in types and (t.crew_size or 0) > 0]
    out: list[Finding] = []

    for lf in eng.ds.lifts:
        lt = eng.ds.task_by_id[lf.task_id]
        path = [(lf.pickup[0], lf.pickup[1]), (lf.dropoff[0], lf.dropoff[1])]
        width = max(corridor_w, G.equivalent_diameter_m(lf.area_m2) + 3.0)
        z_lvl = lf.dropoff[2]
        for t in occupied:
            if t.task_id == lt.task_id:
                continue
            ov = eng.overlapping(lt, t)
            if ov is None:
                continue
            zt = eng.ds.zone_by_id[t.zone_id]
            hit = G.zones_crossed_by_path([zt], path, width, z_level=None)
            if not hit:
                continue
            if not (zt.z_floor - 1.0 <= z_lvl <= zt.z_top + 40.0):
                continue  # груз проходит существенно выше/ниже — не «над людьми»
            dur = (ov[1] - ov[0]).total_seconds() / 3600.0
            out.append(eng.mk(
                rule,
                f"Траектория подъёма {lf.lift_id} ({lf.load_name}, {lf.mass_kg:.0f} кг) проходит над зоной "
                f"{zt.zone_id}, где в это время находятся {t.crew_size} чел. ({t.task_id}). "
                f"Совпадение {dur:.1f} ч, коридор {width:.1f} м.",
                start=ov[0], end=ov[1], zone_ids=[zt.zone_id], task_ids=[lt.task_id, t.task_id],
                crew_ids=[t.crew_id], people_exposed=t.crew_size or 0,
                location=(lf.dropoff[0], lf.dropoff[1], z_lvl), lift_id=lf.lift_id,
                metrics={"corridor_width_m": round(width, 2), "hours": round(dur, 2),
                         "crossed_area_m2": round(hit[0][1], 1) if math.isfinite(hit[0][1]) else None},
            ))
    return out


# ---------------------------------------------------------------------------
# точка входа
# ---------------------------------------------------------------------------
def analyze(ds: Dataset, cfg: RulesConfig, wind: WindField,
            lift_assessments: Sequence[Any] | None = None,
            rule_ids: Sequence[str] | None = None) -> tuple[RuleEngine, list[Finding], pd.DataFrame]:
    """Прогон всех правил. Возвращает (движок, находки, DataFrame для conflicts.csv)."""
    eng = RuleEngine(ds, cfg, wind)
    if lift_assessments is not None:
        eng.lift_assessments = list(lift_assessments)
    findings, df = eng.run_all() if rule_ids is None else (eng.run(rule_ids), None)
    if df is None:
        df = pd.DataFrame([f.as_row() for f in findings])
    return eng, findings, df
