"""
core/sim/lifts.py — оценка одного подъёма «от и до».

Собирает вместе: данные ППР (lifts.csv), ветер (wind.csv -> WindField),
грузовую характеристику крана, возможности людей на оттяжке и динамику
подвеса. Результат — LiftAssessment с пиковыми значениями, временем
превышения и ОБРАТНЫМИ величинами («безопасно до X м/с»), потому что
проектировщику нужно не только «плохо», но и «до какого ветра можно».
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.config import RulesConfig
from core.io.loaders import CraneSpec, Lift, WorkTask, WindSeries
from core.physics import loads as L
from core.physics.pendulum import pendulum_period, simulate_pendulum, static_deflection_deg
from core.physics.wind import WindField, profile_factor


@dataclass
class LiftAssessment:
    lift: Lift
    task: WorkTask
    window: tuple[pd.Timestamp, pd.Timestamp]
    daf: float
    series: pd.DataFrame
    summary: dict[str, Any]
    crane_capacity_kg: float
    limits: dict[str, Any]
    swing: dict[str, Any] = field(default_factory=dict)
    violations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def peak_wind_force_N(self) -> float:
        return float(self.summary.get("wind_force_max_N", 0.0))

    @property
    def peak_v_work_ms(self) -> float:
        return float(self.summary.get("v_work_max_ms", 0.0))

    @property
    def peak_v_ref_ms(self) -> float:
        return float(self.summary.get("v_ref_max_ms", 0.0))

    def as_dict(self) -> dict[str, Any]:
        return {
            "lift_id": self.lift.lift_id,
            "task_id": self.task.task_id,
            "load_name": self.lift.load_name,
            "window": [self.window[0].strftime("%Y-%m-%d %H:%M"), self.window[1].strftime("%Y-%m-%d %H:%M")],
            "input": self.lift.to_dict(),
            "daf": self.daf,
            "crane_capacity_kg": self.crane_capacity_kg,
            "limits": self.limits,
            "summary": self.summary,
            "swing": self.swing,
            "violations": self.violations,
            "warnings": self.warnings,
        }


def _daf_from(cfg: RulesConfig, mode: str) -> float:
    tbl = cfg.physics.get("dynamic_amplification", {}) or {}
    return float(tbl.get(mode, tbl.get("normal", 1.2)))


def assess_lift(lift: Lift, task: WorkTask, wind: WindField, crane: CraneSpec,
                cfg: RulesConfig, *, run_swing: bool = True, swing_engine: str = "analytic",
                swing_window_s: float = 180.0, step_min: float = 15.0, zones=None) -> LiftAssessment:
    """Полная оценка одного подъёма."""
    people = cfg.people
    g = cfg.g
    daf = _daf_from(cfg, lift.daf_mode)
    capacity_kg = crane.capacity_kg(lift.radius_m)

    margin = float(cfg.param("tagline_force_within_capacity", "margin", 1.0))
    max_sling_util = float(cfg.param("sling_utilization_limit", "max_utilization", 0.8))
    max_crane_util = float(cfg.param("crane_radius_within_chart", "max_utilization", 0.9))
    included_limit = float(cfg.physics.get("sling_included_angle_limit_deg", 90.0))

    series = L.evaluate_lift_series(
        lift, wind, daf=daf, crane_capacity_kg=capacity_kg, people_cfg=people,
        step_min=step_min, start=task.start, end=task.end, g=g,
    )
    summary = L.series_summary(series, lift.wind_limit_ms)
    summary["v_ref_max_ms"] = float(series["v_ref_ms"].max())
    summary["v_ref_mean_ms"] = float(series["v_ref_ms"].mean())

    # --- обратные величины: «безопасно до ...» -------------------------
    f_person = float(people.get("tagline_force_per_person_N", 200.0))
    eff = float(people.get("tagline_effectiveness", 0.7))
    tag_cap_N = L.tagline_capacity_N(lift.tagline_people, f_person, eff)
    v_safe_tagline = L.max_wind_speed_for_capacity(tag_cap_N / margin, lift.area_m2, lift.cd_max, wind.rho)
    # Предел по стропам и по крану от ветра не зависит: это статические
    # проверки массы и схемы строповки, поэтому «безопасная скорость ветра»
    # определяется только оттяжкой и лимитом ППР.
    limits = {
        "wind_limit_ms_from_ppr": lift.wind_limit_ms,
        "crane_wind_limit_ops_ms": crane.wind_limit_ops_ms,
        "crane_wind_limit_climbing_ms": crane.wind_limit_climbing_ms,
        "tagline_capacity_N": round(tag_cap_N, 1),
        "tagline_margin": margin,
        "max_sling_utilization": max_sling_util,
        "max_crane_utilization": max_crane_util,
        "sling_included_angle_limit_deg": included_limit,
        "safe_wind_ms_by_tagline_at_work_height": round(v_safe_tagline, 2) if math.isfinite(v_safe_tagline) else None,
        "safe_wind_ms_by_tagline_at_ref_height": (round(v_safe_tagline / profile_factor(lift.hook_height_m, wind.ref_height_m, wind.alpha), 2)
                                                  if math.isfinite(v_safe_tagline) else None),
        "daf": daf,
        "rho_kg_m3": round(wind.rho, 4),
        "height_m": lift.hook_height_m,
    }

    # --- проверка схемы строповки --------------------------------------
    included = L.sling_included_angle_deg(lift.sling_angle_deg, lift.sling_legs)
    K = L.sling_leg_factor(lift.sling_angle_deg)
    summary["sling_included_angle_deg"] = round(included, 2)
    summary["sling_leg_factor_K"] = round(K, 4)
    summary["crane_capacity_kg_at_radius"] = round(capacity_kg, 1)

    # --- окна, когда подъём допустим («когда МОЖНО», а не только «нельзя») --
    if lift.tagline_people > 0 and math.isfinite(v_safe_tagline):
        v_tag_ref = v_safe_tagline / profile_factor(lift.hook_height_m, wind.ref_height_m, wind.alpha)
        v_limit_for_window = min(lift.wind_limit_ms, v_tag_ref)
    else:
        # оттяжка не назначена (груз удерживается не людьми, а приспособлением):
        # лимит по оттяжке не применяется, остаётся лимит ППР/паспорта.
        v_limit_for_window = lift.wind_limit_ms
    safe_windows = find_safe_windows(
        wind, task, lift, cfg,
        v_limit_ref=v_limit_for_window,
        search_start=task.start - pd.Timedelta(days=2),
        search_end=task.end + pd.Timedelta(days=2),
    )
    summary["safe_windows"] = safe_windows

    # --- динамика подвеса ---------------------------------------------
    swing: dict[str, Any] = {}
    if run_swing:
        swing = _run_swing(lift, task, wind, cfg, tag_cap_N, engine=swing_engine,
                           window_s=swing_window_s, zones=zones)

    # --- нарушения ------------------------------------------------------
    violations: list[dict[str, Any]] = []
    warnings: list[str] = []

    def add(rule_id: str, message: str, **metric: Any) -> None:
        r = cfg.rule(rule_id)
        violations.append({"rule_id": rule_id, "title": r.title, "severity": r.severity,
                           "source": r.source, "message": message, "metric": metric})

    peak = series.loc[series["tagline_deficit_N"].idxmax()] if len(series) else None
    if peak is not None and float(peak["tagline_deficit_N"]) > 0 and \
            cfg.rule("tagline_force_within_capacity").enabled:
        if lift.tagline_people <= 0:
            msg = (f"Средства удержания груза не назначены (tagline_people=0), а ветровое усилие "
                   f"достигает {peak['tagline_required_N']:.0f} Н. Груз ничем не контролируется "
                   f"на высоте {lift.hook_height_m:.0f} м.")
        else:
            msg = (f"Требуется {peak['tagline_required_N']:.0f} Н на оттяжке, доступно {tag_cap_N:.0f} Н "
                   f"({lift.tagline_people} чел.). Дефицит {peak['tagline_deficit_N']:.0f} Н.")
        add("tagline_force_within_capacity", msg,
            required_N=round(float(peak["tagline_required_N"]), 1),
            capacity_N=round(tag_cap_N, 1),
            deficit_N=round(float(peak["tagline_deficit_N"]), 1),
            at=str(peak["timestamp"]),
            tagline_people=lift.tagline_people,
            v_work_ms=round(float(peak["v_work_ms"]), 2))

    u_sl = float(summary["sling_utilization_max"])
    if u_sl > max_sling_util and cfg.rule("sling_utilization_limit").enabled:
        add("sling_utilization_limit",
            f"Использование стропа {u_sl:.0%} при лимите {max_sling_util:.0%} "
            f"(WLL {lift.sling_wll_kg:.0f} кг, {lift.carrying_legs} несущих ветви, угол {lift.sling_angle_deg:.0f}°).",
            utilization=round(u_sl, 4), limit=max_sling_util,
            tension_N=round(float(summary["sling_tension_max_N"]), 1),
            wll_kg=lift.sling_wll_kg)

    u_cr = summary.get("crane_utilization_max")
    if u_cr is not None and math.isfinite(u_cr) and u_cr > max_crane_util and \
            cfg.rule("crane_radius_within_chart").enabled:
        add("crane_radius_within_chart",
            f"Нагрузка на кран {u_cr:.0%} от грузовой характеристики на вылете {lift.radius_m:.1f} м "
            f"({capacity_kg:.0f} кг). Масса груза {lift.mass_kg:.0f} кг, DAF {daf}.",
            utilization=round(float(u_cr), 4), limit=max_crane_util,
            radius_m=lift.radius_m, capacity_kg=round(capacity_kg, 1))

    if lift.wind_limit_ms < float("inf") and summary["wind_limit_exceeded_pct"] > 0 and \
            cfg.rule("stop_crane_ops_above_wind_limit").enabled:
        add("stop_crane_ops_above_wind_limit",
            f"Ветер выше лимита ППР {lift.wind_limit_ms:.0f} м/с в течение "
            f"{summary['wind_limit_exceeded_hours']:.1f} ч ({summary['wind_limit_exceeded_pct']:.0f}% окна подъёма).",
            limit_ms=lift.wind_limit_ms,
            exceeded_hours=round(float(summary["wind_limit_exceeded_hours"]), 2),
            exceeded_pct=round(float(summary["wind_limit_exceeded_pct"]), 1),
            v_ref_max_ms=round(float(summary["v_ref_max_ms"]), 2),
            v_work_max_ms=round(float(summary["v_work_max_ms"]), 2),
            basis=wind_basis_name(cfg))

    if included > included_limit:
        warnings.append(f"Угол между ветвями {included:.0f}° больше типового предела {included_limit:.0f}°. "
                        f"Сверьте с таблицами производителя стропа: при большом растворе WLL снижается сильнее, "
                        f"чем даёт геометрия 1/sin(a).")
    if lift.cd_min == lift.cd_max:
        warnings.append("Cd задан одним числом. Для плоских панелей корректнее диапазон (например 1.1-1.6) — "
                        "см. справочник по ветровой нагрузке.")
    if math.isfinite(v_safe_tagline) and v_safe_tagline < 3.0:
        warnings.append(f"Расчётная предельная скорость ветра по оттяжке всего {v_safe_tagline:.1f} м/с на высоте "
                        f"{lift.hook_height_m:.0f} м: удержание силами {lift.tagline_people} чел. практически "
                        f"невозможно уже при слабом ветре. Рассмотрите траверсу/захват или уменьшение парусности.")

    return LiftAssessment(lift=lift, task=task, window=(task.start, task.end), daf=daf,
                          series=series, summary=summary, crane_capacity_kg=capacity_kg,
                          limits=limits, swing=swing, violations=violations, warnings=warnings)


def find_safe_windows(wind: WindField, task: WorkTask, lift: Lift, cfg: RulesConfig, *,
                      v_limit_ref: float, search_start, search_end,
                      step_h: float = 1.0, min_duration_h: float = 1.0) -> list[dict[str, Any]]:
    """Окна, в которые подъём допустим по ветру.

    Зачем: вывод «нельзя» бесполезен для планировщика. Нужен ответ
    «можно в эти окна, суммарно N часов, ближайшее — тогда-то».
    Лимит берётся на базисе паспорта (высота 10 м), потому что именно к ней
    нормированы и лимит ППР, и оценка по оттяжке.
    """
    if not math.isfinite(v_limit_ref) or v_limit_ref <= 0:
        return []
    times = pd.date_range(search_start, search_end, freq=f"{int(round(step_h))}h")
    ok = []
    for t in times:
        v = wind.gust_at(t, wind.ref_height_m)
        ok.append(bool(v <= v_limit_ref) and wind.covers(t))
    windows: list[dict[str, Any]] = []
    i = 0
    n = len(ok)
    while i < n:
        if not ok[i]:
            i += 1
            continue
        j = i
        while j < n and ok[j]:
            j += 1
        t0, t1 = times[i], times[min(j, n - 1)] + pd.Timedelta(hours=step_h)
        dur = (t1 - t0).total_seconds() / 3600.0
        if dur >= min_duration_h:
            windows.append({
                "start": t0.strftime("%Y-%m-%d %H:%M"), "end": t1.strftime("%Y-%m-%d %H:%M"),
                "hours": round(dur, 1),
                "in_task_window": bool(t0 <= task.end and t1 >= task.start),
                "shift_from_task_start_h": round((t0 - task.start).total_seconds() / 3600.0, 1),
                "limit_ref_ms": round(v_limit_ref, 2),
            })
        i = j
    return windows


def wind_basis_name(cfg: RulesConfig) -> str:
    return str(cfg.meta.get("wind_limit_basis", "ref_height"))


def _collision_boxes_from_zones(zones) -> list[dict[str, Any]]:
    levels: dict[str, list[Any]] = {}
    for zone in zones or []:
        levels.setdefault(zone.level, []).append(zone)
    boxes = []
    for level, items in levels.items():
        z0 = min(zone.z_floor for zone in items)
        x0 = min(zone.x_min for zone in items) - 2
        x1 = max(zone.x_max for zone in items) + 2
        y0 = min(zone.y_min for zone in items) - 2
        y1 = max(zone.y_max for zone in items) + 2
        boxes.append({
            "name": f"Slab_{level}",
            "center": [(x0 + x1) / 2, (y0 + y1) / 2, z0 - 0.225],
            "size": [(x1 - x0) / 2, (y1 - y0) / 2, 0.225],
        })
    core = [zone for zone in zones or [] if zone.type == "work" and "CORE" in zone.zone_id]
    if core:
        zone = max(core, key=lambda item: item.z_floor)
        ztop = max(item.z_top for item in zones)
        boxes.append({
            "name": "Core",
            "center": [
                (zone.x_min + zone.x_max) / 2,
                (zone.y_min + zone.y_max) / 2,
                (zone.z_floor - 4 + ztop + 6) / 2,
            ],
            "size": [
                (zone.x_max - zone.x_min) / 2,
                (zone.y_max - zone.y_min) / 2,
                (ztop + 10 - zone.z_floor) / 2,
            ],
        })
    return boxes


def _run_swing(lift: Lift, task: WorkTask, wind: WindField, cfg: RulesConfig,
               tag_cap_N: float, engine: str = "analytic", window_s: float = 180.0,
               zones=None) -> dict[str, Any]:
    """Раскачка груза в окне максимального порыва внутри интервала подъёма."""
    z = lift.hook_height_m
    # момент максимального порыва в окне задачи
    idx = wind.gust_ms * profile_factor(z, wind.ref_height_m, wind.alpha)
    mask = np.asarray((wind.ts >= task.start) & (wind.ts <= task.end))
    if not mask.any():
        t_peak = task.start + (task.end - task.start) / 2
    else:
        vals = np.where(mask, idx, -np.inf)
        t_peak = wind.ts[int(np.argmax(vals))]

    moving = bool(cfg.physics.get("moving_anchor", False))
    with_collisions = bool(cfg.physics.get("collision_geometry", False))
    if (moving or with_collisions) and engine != "mujoco":
        raise ValueError("moving_anchor и collision_geometry требуют --swing-engine mujoco")

    if moving:
        from core.sim.timeline import LoadTrajectory
        trajectory = LoadTrajectory(lift, task)
        t0 = task.start
        duration = (task.end - task.start).total_seconds()

        def anchor_at(sec: float):
            state = trajectory.at(t0 + pd.Timedelta(seconds=float(sec)))
            return state["x"], state["y"], state["z"] + lift.suspend_length_m
    else:
        t0 = t_peak - pd.Timedelta(seconds=window_s / 2.0)
        duration = window_s
        anchor_at = None

    def v_of_t(sec: float) -> float:
        return wind.gust_at(t0 + pd.Timedelta(seconds=float(sec)), z)

    collision_boxes = _collision_boxes_from_zones(zones) if with_collisions else []
    res = simulate_pendulum(
        wind_speed_fn=v_of_t, duration_s=duration, mass_kg=lift.mass_kg,
        length_m=lift.suspend_length_m, area_m2=lift.area_m2, cd=lift.cd_mid,
        rho=wind.rho, anchor=(lift.dropoff[0], lift.dropoff[1], lift.hook_height_m),
        wind_dir_deg=200.0, tagline_capacity_N=tag_cap_N,
        size_m=(max(0.3, math.sqrt(lift.area_m2) / 2), 0.12, max(0.6, math.sqrt(lift.area_m2))),
        timestep=float(cfg.physics.get("moving_timestep_s", 0.02)) if moving else (0.004 if engine == "mujoco" else 0.01),
        sample_every_s=float(cfg.physics.get("moving_sample_s", 1.0)) if moving else 0.25,
        initial_theta_deg=1.0, engine=engine, g=cfg.g,
        anchor_fn=anchor_at, collision_boxes=collision_boxes,
    )

    T0 = pendulum_period(lift.suspend_length_m, cfg.g)
    measured = None if moving else res.measured_period_s()
    F_peak = float(np.max(res.wind_force_N)) if len(res.wind_force_N) else 0.0
    structural = [event for event in res.collisions if event["name"] != "Slab_G"]
    ground = [event for event in res.collisions if event["name"] == "Slab_G"]
    return {
        "engine": engine,
        "window_s": duration,
        "requested_window_s": window_s,
        "window_start": t0.strftime("%Y-%m-%d %H:%M:%S"),
        "peak_time": t_peak.strftime("%Y-%m-%d %H:%M"),
        "suspend_length_m": lift.suspend_length_m,
        "natural_period_s": round(T0, 3),
        "measured_period_s": round(measured, 3) if measured else None,
        "moving_anchor": moving,
        "collision_boxes": len(collision_boxes),
        "collision_count": len(res.collisions),
        "collisions": res.collisions,
        "structural_collision_count": len(structural),
        "structural_collisions": structural,
        "ground_contact_count": len(ground),
        "ground_contacts": ground,
        "contact_max_normal_force_N": res.meta["contact_max_normal_force_N"],
        "contact_max_impact_speed_m_s": max((event["max_impact_speed_m_s"] for event in structural), default=0.0),
        "contact_force_converged": False,
        "dynamics_note": "full_operation_moving_anchor" if moving else "fixed_anchor_peak_window",
        "static_deflection_deg_at_peak": round(static_deflection_deg(F_peak, lift.mass_kg, cfg.g), 3),
        "max_theta_deg": round(res.max_theta_deg, 3),
        "max_offset_m": round(res.max_offset_m, 3),
        "steady_offset_m": round(res.steady_offset_m, 3),
        "max_tension_N": round(res.max_tension_N, 1),
        "peak_wind_ms": round(float(np.max(res.wind_ms)), 2) if len(res.wind_ms) else None,
        "peak_wind_force_N": round(F_peak, 1),
        "tagline_capacity_N": round(tag_cap_N, 1),
        "track": res.track_json(every=1),
    }


def assess_all_lifts(ds, wind: WindField, cfg: RulesConfig, *, run_swing: bool = True,
                     swing_engine: str = "analytic", step_min: float = 15.0) -> list[LiftAssessment]:
    out = []
    for lift in ds.lifts:
        task = ds.task_by_id[lift.task_id]
        out.append(assess_lift(lift, task, wind, ds.crane, cfg, run_swing=run_swing,
                               swing_engine=swing_engine, step_min=step_min, zones=ds.zones))
    return out
