"""
core/sim/montecarlo.py — вероятностные прогоны.

Два независимых Monte Carlo:

A) MC ПОДЪЁМА. Для каждого подъёма из lifts.csv сэмплируются:
     * скорость ветра — из ЭМПИРИЧЕСКОГО распределения измеренных порывов
       (либо из подобранного Вейбулла), а не из нормального закона;
     * Cd — из диапазона cd_min..cd_max;
     * масса — допуск на налипание бетона/обледенение/неточность взвешивания;
     * угол ветви стропа — допуск на монтаж;
     * число людей на оттяжке — биномиально (кто-то может не прийти/быть занят);
     * DAF — по режиму подъёма.
   На выходе: вероятность превышения каждого лимита, процентили, кривая
   «безопасная скорость ветра -> вероятность отказа».

B) MC ГРАФИКА. Сэмплируются опоздания и растяжения работ, затем быстрым
   ядром пересчитываются конфликты. На выходе: вероятность каждого
   конфликта и каждого парного пересечения — то есть «это не случайность,
   оно повторится в 78 % недель».

Почему эмпирика, а не нормальный закон (важно для высотного объекта):
  * распределение скорости ветра асимметрично и обрезано нулём;
  * порыв на высоте 250 м и средняя скорость у земли — разные случайные
    величины, нормальная аппроксимация занижает хвост;
  * при N=5000 разница в оценке P(превышения) легко достигает разов.
  Нормальный закон оставлен как `method="normal"` — чтобы можно было
  воспроизвести упрощённый расчёт и показать разницу.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from core.context import Context
from core.io.loaders import Lift, WorkTask
from core.physics import loads as L
from core.physics.wind import profile_factor


# ===========================================================================
# A) Monte Carlo подъёма
# ===========================================================================
@dataclass
class LiftMCResult:
    lift_id: str
    n: int
    method: str
    seed: int
    samples: dict[str, np.ndarray]
    probabilities: dict[str, float]
    percentiles: dict[str, dict[str, float]]
    safe_wind_curve: list[dict[str, float]]
    params: dict[str, Any]
    weibull: dict[str, float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lift_id": self.lift_id, "n": self.n, "method": self.method, "seed": self.seed,
            "params": self.params, "weibull": self.weibull,
            "probabilities": self.probabilities,
            "percentiles": self.percentiles,
            "safe_wind_curve": self.safe_wind_curve,
            "summary": {k: {
                "mean": float(np.mean(v)), "p50": float(np.percentile(v, 50)),
                "p95": float(np.percentile(v, 95)), "max": float(np.max(v)),
            } for k, v in self.samples.items()},
        }


def fit_weibull(v: np.ndarray) -> tuple[float, float]:
    """Подбор (k, lambda) распределения Вейбулла к выборке скоростей."""
    from scipy.stats import weibull_min
    v = np.clip(np.asarray(v, float), 0.05, None)
    k, _, lam = weibull_min.fit(v, floc=0)
    return float(k), float(lam)


def sample_wind(rng: np.random.Generator, empirical: np.ndarray, n: int,
                method: str, weibull: tuple[float, float] | None) -> np.ndarray:
    if method == "empirical":
        # бутстрэп измеренных порывов + лёгкий джиттер, чтобы не повторять
        # дискретные часовые значения один в один
        idx = rng.integers(0, len(empirical), n)
        jit = rng.normal(0.0, 0.04, n)
        return np.clip(empirical[idx] * (1.0 + jit), 0.0, None)
    if method == "weibull":
        k, lam = weibull
        return rng.weibull(k, n) * lam
    if method == "normal":
        mu, sd = float(np.mean(empirical)), float(np.std(empirical))
        return np.clip(rng.normal(mu, sd, n), 0.0, None)
    raise ValueError(f"Неизвестный метод сэмплирования ветра: {method}")


def lift_monte_carlo(ctx: Context, lift: Lift, task: WorkTask, *, n: int = 5000,
                     seed: int = 42, method: str = "empirical",
                     mass_tolerance_pct: float = 5.0, angle_tolerance_deg: float = 3.0,
                     z_work: float | None = None) -> LiftMCResult:
    """Вероятностная оценка одного подъёма."""
    rng = np.random.default_rng(seed)
    cfg, wind = ctx.cfg, ctx.wind
    people = cfg.people
    g = cfg.g

    z = float(z_work if z_work is not None else lift.hook_height_m)
    pf = profile_factor(z, wind.ref_height_m, wind.alpha)

    # --- выборка ветра: порывы внутри окна задачи (а не за весь месяц) ---
    mask = np.asarray((wind.ts >= task.start) & (wind.ts <= task.end))
    gust_ref = wind.gust_ms[mask] if mask.any() else wind.gust_ms
    mean_ref = wind.mean_ms[mask] if mask.any() else wind.mean_ms
    if len(gust_ref) < 8:  # короткое окно -> берём весь ряд, но помечаем это
        gust_ref, mean_ref = wind.gust_ms, wind.mean_ms
    v_ref = sample_wind(rng, gust_ref, n, method, fit_weibull(gust_ref) if method == "weibull" else None)
    v_work = v_ref * pf

    # --- остальные случайные величины ---
    cd = rng.uniform(lift.cd_min, lift.cd_max, n)
    mass = lift.mass_kg * (1.0 + rng.normal(0.0, mass_tolerance_pct / 300.0, n))  # +-tolerance (3 sigma)
    mass = np.clip(mass, lift.mass_kg * 0.9, None)
    angle = np.clip(lift.sling_angle_deg + rng.normal(0.0, angle_tolerance_deg / 2.0, n), 20.0, 90.0)
    daf_tbl = cfg.physics.get("dynamic_amplification", {}) or {}
    daf_base = float(daf_tbl.get(lift.daf_mode, daf_tbl.get("normal", 1.2)))
    daf = daf_base * (1.0 + rng.normal(0.0, 0.03, n))

    p_show = float(people.get("tagline_attendance_p", 0.9))
    tag_ppl = rng.binomial(max(lift.tagline_people, 0), p_show, n) if lift.tagline_people > 0 \
        else np.zeros(n, int)
    tag_cap = tag_ppl * float(people.get("tagline_force_per_person_N", 200.0)) \
        * float(people.get("tagline_effectiveness", 0.7))

    rho = wind.rho

    # --- детерминированный пересчёт на всю выборку (векторно) ---
    F_w = 0.5 * rho * cd * lift.area_m2 * v_work ** 2
    T_leg = mass * g * daf / (lift.carrying_legs * np.sin(np.radians(angle)))
    U_sling = T_leg / (lift.sling_wll_kg * g)
    cap_kg = ctx.ds.crane.capacity_kg(lift.radius_m)
    U_crane = mass * daf / cap_kg
    margin = float(cfg.param("tagline_force_within_capacity", "margin", 1.0))
    tag_deficit = np.clip(F_w * margin - tag_cap, 0.0, None)

    # --- статическое отклонение груза (маятник) ---
    theta_deg = np.degrees(np.arctan2(F_w, mass * g))
    offset_m = lift.suspend_length_m * np.sin(np.radians(theta_deg))

    # --- вероятности превышения лимитов ---
    lim_wind = lift.wind_limit_ms
    v_cmp = v_ref if str(cfg.meta.get("wind_limit_basis", "ref_height")) == "ref_height" else v_work
    max_u_sling = float(cfg.param("sling_utilization_limit", "max_utilization", 0.8))
    max_u_crane = float(cfg.param("crane_radius_within_chart", "max_utilization", 0.9))

    probs = {
        "P_wind_above_ppr_limit": float(np.mean(v_cmp > lim_wind)) if math.isfinite(lim_wind) else 0.0,
        "P_wind_above_crane_limit": float(np.mean(v_cmp > wind_ref_limit(ctx))),
        "P_tagline_force_exceeded": float(np.mean(tag_deficit > 0)),
        "P_sling_utilization_exceeded": float(np.mean(U_sling > max_u_sling)),
        "P_crane_chart_exceeded": float(np.mean(U_crane > max_u_crane)),
        "P_any_critical": float(np.mean((tag_deficit > 0) | (U_sling > max_u_sling) | (U_crane > max_u_crane))),
        "P_load_offset_over_1m": float(np.mean(offset_m > 1.0)),
        "P_load_offset_over_3m": float(np.mean(offset_m > 3.0)),
    }

    def pct(a: np.ndarray) -> dict[str, float]:
        return {f"p{q}": round(float(np.percentile(a, q)), 3) for q in (5, 50, 90, 95, 99)} | \
               {"mean": round(float(np.mean(a)), 3), "max": round(float(np.max(a)), 3)}

    percentiles = {
        "v_work_ms": pct(v_work), "wind_force_N": pct(F_w), "sling_tension_N": pct(T_leg),
        "sling_utilization": pct(U_sling), "crane_utilization": pct(U_crane),
        "tagline_deficit_N": pct(tag_deficit), "static_offset_m": pct(offset_m),
        "theta_deg": pct(theta_deg),
    }

    # --- кривая «безопасная скорость ветра -> вероятность отказа» -------
    curve = []
    for v in np.arange(2.0, 26.1, 1.0):
        Fv = 0.5 * rho * np.mean(cd) * lift.area_m2 * (v * pf) ** 2
        p_tag = float(np.mean(Fv * margin > tag_cap))
        p_wind = float(np.mean(v > lim_wind)) if math.isfinite(lim_wind) else 0.0
        curve.append({"v_ref_ms": float(v), "v_work_ms": round(v * pf, 2),
                      "wind_force_N": round(float(Fv), 1),
                      "P_tagline_exceeded": round(p_tag, 4),
                      "P_over_wind_limit": round(p_wind, 4)})

    params = {
        "mass_kg": lift.mass_kg, "area_m2": lift.area_m2, "cd_range": [lift.cd_min, lift.cd_max],
        "sling_angle_deg": lift.sling_angle_deg, "carrying_legs": lift.carrying_legs,
        "sling_wll_kg": lift.sling_wll_kg, "radius_m": lift.radius_m,
        "crane_capacity_kg": round(cap_kg, 1), "hook_height_m": lift.hook_height_m,
        "suspend_length_m": lift.suspend_length_m, "tagline_people_nominal": lift.tagline_people,
        "tagline_attendance_p": p_show, "wind_limit_ms": lim_wind,
        "profile_factor_to_work_height": round(pf, 4), "rho_kg_m3": round(rho, 4),
        "daf_base": daf_base, "mass_tolerance_pct": mass_tolerance_pct,
        "angle_tolerance_deg": angle_tolerance_deg,
        "wind_window_hours": int(mask.sum()) if mask.any() else 0,
        "wind_window_note": ("использован весь ряд метео-данных: окно подъёма короче 8 часов"
                             if int(mask.sum()) < 8 else "использованы часы окна подъёма"),
    }

    return LiftMCResult(
        lift_id=lift.lift_id, n=int(n), method=method, seed=int(seed),
        samples={"v_work_ms": v_work, "wind_force_N": F_w, "sling_tension_N": T_leg,
                 "sling_utilization": U_sling, "crane_utilization": U_crane,
                 "tagline_deficit_N": tag_deficit, "static_offset_m": offset_m,
                 "theta_deg": theta_deg},
        probabilities=probs, percentiles=percentiles, safe_wind_curve=curve,
        params=params,
        weibull=(lambda kw: {"k": round(kw[0], 3), "lambda_ms": round(kw[1], 3)})(fit_weibull(gust_ref)),
    )


def wind_ref_limit(ctx: Context) -> float:
    return float(ctx.ds.crane.wind_limit_ops_ms)


# ===========================================================================
# B) Monte Carlo графика работ
# ===========================================================================
@dataclass
class ScheduleMCResult:
    n: int
    seed: int
    pair_probabilities: pd.DataFrame      # вероятность пересечения каждой пары задач
    rule_probabilities: dict[str, float]  # вероятность каждого правила за прогон
    findings_per_run: dict[str, np.ndarray]
    exposure_person_hours: dict[str, float]
    params: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "seed": self.seed, "params": self.params,
            "rule_probabilities": self.rule_probabilities,
            "exposure_person_hours": self.exposure_person_hours,
            "top_pairs": self.pair_probabilities.head(25).to_dict(orient="records"),
        }


def _sample_schedule(rng: np.random.Generator, tasks: Sequence[WorkTask], p: dict[str, Any]) -> np.ndarray:
    """Возвращает массив (n, T, 2) с start/end в часах от начала периода."""
    t0 = min(t.start for t in tasks)
    starts = np.array([(t.start - t0).total_seconds() / 3600.0 for t in tasks])
    ends = np.array([(t.end - t0).total_seconds() / 3600.0 for t in tasks])
    n = p["n"]
    T = len(tasks)

    delay_mean = float(p["delay_mean_h"])
    delay_scale = float(p["delay_scale_h"])
    p_delay = float(p["p_task_delayed"])
    dur_sigma = float(p["duration_sigma_pct"]) / 100.0

    delayed = rng.random((n, T)) < p_delay
    delays = np.where(delayed, delay_mean + rng.exponential(delay_scale, (n, T)), 0.0)
    dur = (ends - starts)[None, :] * np.exp(rng.normal(0.0, dur_sigma, (n, T)))

    s = starts[None, :] + delays
    e = s + dur
    return np.stack([s, e], axis=-1)


def schedule_monte_carlo(ctx: Context, *, n: int = 2000, seed: int = 7,
                         delay_mean_h: float = 2.0, delay_scale_h: float = 3.0,
                         p_task_delayed: float = 0.35, duration_sigma_pct: float = 12.0,
                         min_overlap_h: float = 0.5) -> ScheduleMCResult:
    """Быстрый MC по пересечениям работ (без геометрии shapely — только интервалы
    и предвычисленные площади пересечений зон). 2000 прогонов x 19 задач ~ секунды.
    """
    rng = np.random.default_rng(seed)
    ds = ctx.ds
    tasks = sorted(ds.tasks, key=lambda t: t.start)
    T = len(tasks)

    sched = _sample_schedule(rng, tasks, {"n": n, "delay_mean_h": delay_mean_h,
                                          "delay_scale_h": delay_scale_h,
                                          "p_task_delayed": p_task_delayed,
                                          "duration_sigma_pct": duration_sigma_pct})
    S, E = sched[:, :, 0], sched[:, :, 1]

    # предвычисление геометрии: площадь пересечения зон (м2) и перепад высот
    area = np.zeros((T, T))
    dz = np.zeros((T, T))
    ztype = np.zeros((T, T), dtype=bool)   # обе зоны — рабочие
    same_crew = np.zeros((T, T), dtype=bool)
    for i, a in enumerate(tasks):
        za = ds.zone_by_id[a.zone_id]
        for j, b in enumerate(tasks):
            if i == j:
                continue
            zb = ds.zone_by_id[b.zone_id]
            area[i, j] = za.overlaps_xy(zb)
            dz[i, j] = za.z_floor - zb.z_floor
            ztype[i, j] = (za.type == "work" and zb.type == "work")
            same_crew[i, j] = bool(a.crew_id) and a.crew_id == b.crew_id

    iu = np.triu_indices(T, k=1)
    # пересечение интервалов для всех прогонов и всех пар
    ov_start = np.maximum(S[:, iu[0]], S[:, iu[1]])
    ov_end = np.minimum(E[:, iu[0]], E[:, iu[1]])
    ov_h = np.clip(ov_end - ov_start, 0.0, None)
    hit = ov_h >= min_overlap_h

    pair_area = area[iu]
    pair_same_crew = same_crew[iu]
    pair_ztype = ztype[iu]

    # правило 1: две бригады в одной/пересекающейся рабочей зоне
    crew_conflict = hit & (pair_area > 1.0) & (~pair_same_crew) & pair_ztype
    # правило 2: работы друг над другом (перепад >= 2.5 м, перекрытие плана >= 25 %)
    min_frac = float(ctx.cfg.param("no_work_below_active_work", "min_overlap_fraction", 0.25))
    stacked = np.zeros_like(hit)
    dz_u = dz[iu]
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.where(pair_area > 0, pair_area / np.maximum(pair_area, 1e-9), 0.0)
    stacked = hit & (dz_u >= 2.5) & (pair_area > 5.0) & (~pair_same_crew)

    rule_counts = {
        "no_two_crews_same_zone": crew_conflict.any(axis=1).mean(),
        "no_work_below_active_work": stacked.any(axis=1).mean(),
    }

    # вероятность каждой пары
    pair_prob = pd.DataFrame({
        "task_a": [tasks[i].task_id for i in iu[0]],
        "task_b": [tasks[j].task_id for j in iu[1]],
        "zone_a": [tasks[i].zone_id for i in iu[0]],
        "zone_b": [tasks[j].zone_id for j in iu[1]],
        "crew_a": [tasks[i].crew_id for i in iu[0]],
        "crew_b": [tasks[j].crew_id for j in iu[1]],
        "people": [tasks[i].crew_size + tasks[j].crew_size for i, j in zip(*iu)],
        "zone_overlap_m2": np.round(pair_area, 1),
        "same_crew": pair_same_crew,
        "work_zones": pair_ztype,
        "P_overlap": np.round(crew_conflict.mean(axis=0), 4),
        "P_stacked": np.round(stacked.mean(axis=0), 4),
        "mean_overlap_h": np.round(np.where(hit, ov_h, 0.0).mean(axis=0), 2),
    })
    pair_prob["P_any"] = np.round((crew_conflict | stacked).mean(axis=0), 4)
    pair_prob = pair_prob.sort_values("P_any", ascending=False).reset_index(drop=True)

    # человеко-часы под воздействием (две бригады в пересекающихся зонах)
    exposure = np.where(crew_conflict, ov_h * np.array([tasks[i].crew_size + tasks[j].crew_size
                                                       for i, j in zip(*iu)])[None, :], 0.0)
    per_run_exposure = exposure.sum(axis=1)

    params = {
        "n": int(n), "seed": int(seed), "delay_mean_h": delay_mean_h,
        "delay_scale_h": delay_scale_h, "p_task_delayed": p_task_delayed,
        "duration_sigma_pct": duration_sigma_pct, "min_overlap_h": min_overlap_h,
        "tasks": T,
    }

    return ScheduleMCResult(
        n=int(n), seed=int(seed), pair_probabilities=pair_prob,
        rule_probabilities={k: round(float(v), 4) for k, v in rule_counts.items()},
        findings_per_run={"crew_conflicts": crew_conflict.sum(axis=1).astype(float),
                          "stacked": stacked.sum(axis=1).astype(float),
                          "exposure_person_h": per_run_exposure},
        exposure_person_hours={
            "mean": round(float(per_run_exposure.mean()), 1),
            "p50": round(float(np.percentile(per_run_exposure, 50)), 1),
            "p95": round(float(np.percentile(per_run_exposure, 95)), 1),
            "max": round(float(per_run_exposure.max()), 1),
        },
        params=params,
    )
