from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd
from shapely.geometry import box

from .physics import sling_tension, wind_force


def _seed(base: int, text: str) -> int:
    return base + int(hashlib.sha1(text.encode()).hexdigest()[:8], 16) % 1_000_003


def _probability(mask: np.ndarray) -> dict[str, Any]:
    count = int(np.count_nonzero(mask))
    n = len(mask)
    z = 1.959963984540054
    p = count / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return {
        "exceedances": count,
        "probability": p,
        "wilson_95_low": max(0.0, float(center - margin)),
        "wilson_95_high": min(1.0, float(center + margin)),
    }


def wind_monte_carlo(
    lift: pd.Series,
    task: pd.Series,
    wind: pd.DataFrame,
    physics: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    metric = physics.get("wind_metric", "gust_ms")
    source_rows = wind[(wind.timestamp >= task.start) & (wind.timestamp <= task.end)]
    inner = wind[(wind.timestamp > task.start) & (wind.timestamp < task.end)]
    times = pd.DatetimeIndex([task.start]).append(pd.DatetimeIndex(inner.timestamp)).append(pd.DatetimeIndex([task.end]))
    values = np.interp(
        times.astype("int64").to_numpy(dtype=np.int64),
        wind.timestamp.astype("int64").to_numpy(dtype=np.int64),
        wind[metric].to_numpy(dtype=float),
    )
    n = int(config.get("wind_samples", 5000))
    if n < 100:
        raise ValueError("wind_samples должно быть не меньше 100")
    seed = _seed(int(config.get("seed", 42)), str(lift.lift_id))
    rng = np.random.default_rng(seed)
    mean = float(values.mean())
    sigma = float(values.std(ddof=1))
    speeds = np.clip(rng.normal(mean, sigma, n), 0.0, None)
    cd_min = float(lift.get("cd_min", lift.cd))
    cd_max = float(lift.get("cd_max", lift.cd))
    cds = rng.uniform(cd_min, cd_max, n)
    mass_uncertainty = float(config.get("mass_relative_uniform_uncertainty", 0.0))
    masses = float(lift.mass_kg) * rng.uniform(1 - mass_uncertainty, 1 + mass_uncertainty, n)
    forces = wind_force(speeds, float(lift.area_m2), cds, float(physics.get("air_density_kg_m3", 1.225)))
    tensions = sling_tension(
        masses, int(lift.carrying_legs), float(lift.sling_angle_deg),
        float(physics.get("gravity_m_s2", 9.80665)),
    )
    capacity = float(lift.tagline_capacity_n)
    wll_n = float(lift.sling_wll_kg) * float(physics.get("gravity_m_s2", 9.80665))
    wind_limit = float(min(lift.wind_limit_ms, task.wind_limit_ms))
    tagline_exceeded = forces > capacity
    wind_exceeded = speeds > wind_limit
    sling_exceeded = tensions > wll_n
    any_exceeded = tagline_exceeded | wind_exceeded | sling_exceeded
    samples = pd.DataFrame({
        "lift_id": lift.lift_id,
        "wind_ms": speeds,
        "cd": cds,
        "mass_kg": masses,
        "wind_force_n": forces,
        "sling_tension_per_leg_n": tensions,
        "tagline_exceeded": tagline_exceeded,
        "wind_limit_exceeded": wind_exceeded,
        "static_sling_wll_exceeded": sling_exceeded,
    })
    summary = {
        "lift_id": lift.lift_id,
        "samples": n,
        "seed": seed,
        "interpretation": "Вероятность превышения входного порога в этой модели, не вероятность происшествия.",
        "wind_metric": metric,
        "wind_window": {
            "from": task.start.isoformat(), "to": task.end.isoformat(),
            "source_samples": len(source_rows), "analysis_points": len(values),
            "endpoints_linearly_interpolated": True,
        },
        "distributions": {
            "wind": {"kind": "normal_clipped_at_zero", "mean_ms": mean, "std_ms": sigma},
            "cd": {"kind": "uniform", "min": cd_min, "max": cd_max},
            "mass": {"kind": "uniform", "relative_uncertainty": mass_uncertainty},
        },
        "thresholds": {
            "tagline_capacity_n": capacity,
            "operational_wind_limit_ms": wind_limit,
            "static_sling_wll_per_leg_n": wll_n,
        },
        "probabilities": {
            "tagline_capacity": _probability(tagline_exceeded),
            "operational_wind_limit": _probability(wind_exceeded),
            "static_sling_wll": _probability(sling_exceeded),
            "any_defined_threshold": _probability(any_exceeded),
        },
        "wind_force_n_quantiles": {str(q): float(np.quantile(forces, q)) for q in (0.05, 0.5, 0.95)},
        "wind_ms_quantiles": {str(q): float(np.quantile(speeds, q)) for q in (0.05, 0.5, 0.95)},
    }
    return summary, samples


def schedule_monte_carlo(
    works: pd.DataFrame,
    zones: pd.DataFrame,
    lifts: pd.DataFrame,
    config: dict[str, Any],
) -> dict[str, Any]:
    n = int(config.get("schedule_samples", 1000))
    if n < 100:
        raise ValueError("schedule_samples должно быть не меньше 100")
    rng = np.random.default_rng(_seed(int(config.get("seed", 42)), "schedule"))
    task_ids = works.task_id.tolist()
    starts = works.start.astype("int64").to_numpy(dtype=np.int64) / 1e9
    durations = (works.end - works.start).dt.total_seconds().to_numpy(dtype=float)
    delay_seconds = rng.normal(0.0, float(config.get("start_delay_std_min", 5.0)) * 60, (n, len(works)))
    delay_clip = config.get("start_delay_clip_min", [-15.0, 20.0])
    delay_seconds = np.clip(delay_seconds, delay_clip[0] * 60, delay_clip[1] * 60)
    multipliers = rng.normal(1.0, float(config.get("duration_multiplier_std", 0.08)), (n, len(works)))
    duration_clip = config.get("duration_multiplier_clip", [0.75, 1.3])
    multipliers = np.clip(multipliers, duration_clip[0], duration_clip[1])
    sampled_starts = starts[None, :] + delay_seconds
    sampled_ends = sampled_starts + durations[None, :] * multipliers

    zone_by_id = zones.set_index("zone_id")
    polygons = {
        row.zone_id: box(row.x_min, row.y_min, row.x_max, row.y_max)
        for row in zones.itertuples()
    }
    hazards = set(zones.loc[zones.type == "crane_hazard", "zone_id"])
    lift_tasks = set(lifts.task_id)
    pairs: list[dict[str, Any]] = []
    for i in range(len(works)):
        for j in range(i + 1, len(works)):
            a, b = works.iloc[i], works.iloc[j]
            if a.zone_id == b.zone_id and a.crew_id != b.crew_id:
                pairs.append({"kind": "no_two_crews_same_zone", "i": i, "j": j, "zone_id": a.zone_id})
    for crane_id in lift_tasks:
        crane_indices = works.index[works.task_id == crane_id].tolist()
        if not crane_indices:
            continue
        i = works.index.get_loc(crane_indices[0])
        crane = works.iloc[i]
        crane_hazards = [crane.zone_id] if crane.zone_id in hazards else [
            hazard for hazard in hazards if polygons[hazard].intersects(polygons[crane.zone_id])
        ]
        for j, other in works.iterrows():
            if other.task_id == crane_id:
                continue
            for hazard in crane_hazards:
                if polygons[hazard].intersects(polygons[other.zone_id]):
                    pairs.append({
                        "kind": "no_work_under_crane_hazard", "i": i, "j": j,
                        "zone_id": hazard,
                    })

    total = np.zeros(n, dtype=int)
    by_kind = {"no_two_crews_same_zone": np.zeros(n, dtype=int), "no_work_under_crane_hazard": np.zeros(n, dtype=int)}
    pair_rows = []
    for pair in pairs:
        mask = (sampled_starts[:, pair["i"]] < sampled_ends[:, pair["j"]]) & (sampled_starts[:, pair["j"]] < sampled_ends[:, pair["i"]])
        total += mask
        by_kind[pair["kind"]] += mask
        pair_rows.append({
            "rule": pair["kind"], "task_a": task_ids[pair["i"]], "task_b": task_ids[pair["j"]],
            "zone_id": pair["zone_id"], **_probability(mask),
        })
    pair_rows.sort(key=lambda row: row["probability"], reverse=True)
    return {
        "samples": n,
        "interpretation": "Повторяемость временных конфликтов при условных отклонениях графика, не частота аварий.",
        "distributions": {
            "start_delay": {"kind": "normal_clipped", "std_min": float(config.get("start_delay_std_min", 5.0)), "clip_min": delay_clip},
            "duration_multiplier": {"kind": "normal_clipped", "std": float(config.get("duration_multiplier_std", 0.08)), "clip": duration_clip},
        },
        "probability_any_schedule_conflict": _probability(total > 0),
        "probability_by_rule": {kind: _probability(mask > 0) for kind, mask in by_kind.items()},
        "expected_conflicts_per_run": float(total.mean()),
        "p95_conflicts_per_run": float(np.quantile(total, 0.95)),
        "pair_probabilities": pair_rows,
    }
