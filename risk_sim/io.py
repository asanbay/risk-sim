from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

WORK_COLUMNS = {
    "task_id", "name", "zone_id", "start", "end", "crew_id", "crew_size",
    "equipment_id", "hazard_tags", "wind_limit_ms",
}
ZONE_COLUMNS = {
    "zone_id", "level", "x_min", "y_min", "x_max", "y_max",
    "z_floor", "z_top", "type",
}
LIFT_COLUMNS = {
    "lift_id", "task_id", "load_name", "mass_kg", "area_m2", "cd",
    "sling_legs", "sling_angle_deg", "carrying_legs", "sling_wll_kg",
    "hook_height_m", "radius_m", "tagline_people", "wind_limit_ms",
}
RULE_NAMES = {
    "no_two_crews_same_zone", "no_work_under_crane_hazard",
    "edge_work_requires_guardrail", "stop_crane_ops_above_wind_limit",
}


class InputError(ValueError):
    pass


@dataclass
class Inputs:
    works: pd.DataFrame
    zones: pd.DataFrame
    lifts: pd.DataFrame
    wind: pd.DataFrame
    rules: dict[str, Any]
    building: dict[str, Any]
    data_dir: Path


def _require_columns(frame: pd.DataFrame, required: set[str], name: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise InputError(f"{name}: отсутствуют колонки: {', '.join(missing)}")


def _numeric(frame: pd.DataFrame, columns: list[str], name: str) -> None:
    for column in columns:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="raise")


def _boolean(value: Any) -> bool | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "да"}:
        return True
    if normalized in {"false", "0", "no", "нет"}:
        return False
    if not normalized:
        return None
    raise InputError(f"Недопустимое логическое значение guardrail: {value!r}")


def _validate(inputs: Inputs) -> None:
    errors: list[str] = []
    works, zones, lifts, wind = inputs.works, inputs.zones, inputs.lifts, inputs.wind
    if works.empty or zones.empty or lifts.empty or wind.empty:
        errors.append("works, zones, lifts и wind должны содержать хотя бы одну строку")
    for name, frame, key in (("works", works, "task_id"), ("zones", zones, "zone_id"), ("lifts", lifts, "lift_id")):
        if not frame.empty and (frame[key].isna().any() or not frame[key].is_unique):
            errors.append(f"{name}: {key} должен быть заполнен и уникален")
    if not works.empty:
        unknown_zones = set(works.zone_id) - set(zones.zone_id)
        if unknown_zones:
            errors.append(f"works: неизвестные zone_id: {sorted(unknown_zones)}")
        invalid_dates = works.end <= works.start
        if invalid_dates.any():
            errors.append(f"works: end <= start у {works.loc[invalid_dates, 'task_id'].tolist()}")
        if (works.crew_size <= 0).any() or (works.wind_limit_ms <= 0).any():
            errors.append("works: crew_size и wind_limit_ms должны быть положительными")
        for column in ("start", "end"):
            if getattr(works[column].dt, "tz", None) is not None:
                errors.append(f"works: {column} должен быть в локальном времени модели без часового пояса")
    if not lifts.empty:
        unknown_tasks = set(lifts.task_id) - set(works.task_id)
        if unknown_tasks:
            errors.append(f"lifts: неизвестные task_id: {sorted(unknown_tasks)}")
        positive = ["mass_kg", "area_m2", "cd", "sling_wll_kg", "hook_height_m", "radius_m", "wind_limit_ms"]
        if (lifts[positive] <= 0).any().any():
            errors.append(f"lifts: значения {positive} должны быть положительными")
        if ((lifts.sling_angle_deg <= 0) | (lifts.sling_angle_deg > 90)).any():
            errors.append("lifts: sling_angle_deg должен быть в диапазоне (0, 90]")
        if ((lifts.carrying_legs <= 0) | (lifts.carrying_legs > lifts.sling_legs)).any():
            errors.append("lifts: carrying_legs должно быть положительным и не больше sling_legs")
        if (lifts.tagline_people < 0).any():
            errors.append("lifts: tagline_people не может быть отрицательным")
        if "rope_length_m" in lifts and (lifts.rope_length_m <= 0).any():
            errors.append("lifts: rope_length_m должен быть положительным")
        if {"cd_min", "cd_max"}.issubset(lifts):
            if ((lifts.cd_min <= 0) | (lifts.cd_min > lifts.cd) | (lifts.cd > lifts.cd_max)).any():
                errors.append("lifts: требуется 0 < cd_min <= cd <= cd_max")
    if not zones.empty:
        if ((zones.x_min >= zones.x_max) | (zones.y_min >= zones.y_max) | (zones.z_floor >= zones.z_top)).any():
            errors.append("zones: минимальные координаты должны быть меньше максимальных")
    if not wind.empty:
        if wind.timestamp.duplicated().any() or not wind.timestamp.is_monotonic_increasing:
            errors.append("wind: timestamp должен быть уникальным и возрастающим")
        if (wind[["mean_ms", "gust_ms"]] < 0).any().any():
            errors.append("wind: скорость не может быть отрицательной")
        if (wind.gust_ms < wind.mean_ms).any():
            errors.append("wind: gust_ms должен быть не меньше mean_ms")
        if not works.empty and (wind.timestamp.iloc[0] > works.start.min() or wind.timestamp.iloc[-1] < works.end.max()):
            errors.append("wind: данные должны покрывать весь период works")
    for name in RULE_NAMES:
        rule = inputs.rules.get("rules", {}).get(name)
        if not isinstance(rule, dict) or "reference" not in rule or "severity" not in rule:
            errors.append(f"rules.yaml: неполное правило {name}")
    if errors:
        raise InputError("\n".join(f"- {error}" for error in errors))


def load_inputs(data_dir: str | Path) -> Inputs:
    directory = Path(data_dir).resolve()
    works_path = directory / "works.xlsx"
    if works_path.exists():
        works = pd.read_excel(works_path, sheet_name="works")
    elif (directory / "works.csv").exists():
        works = pd.read_csv(directory / "works.csv")
    else:
        raise InputError(f"Не найден {works_path}")
    _require_columns(works, WORK_COLUMNS, "works")
    works = works.copy()
    for column in ("start", "end"):
        works[column] = pd.to_datetime(works[column], errors="raise")
    for column in ("equipment_id", "hazard_tags"):
        works[column] = works[column].fillna("").astype(str)
    _numeric(works, ["crew_size", "wind_limit_ms"], "works")

    zones = pd.read_csv(directory / "zones.csv")
    lifts = pd.read_csv(directory / "lifts.csv")
    wind = pd.read_csv(directory / "wind.csv")
    _require_columns(zones, ZONE_COLUMNS, "zones")
    _require_columns(lifts, LIFT_COLUMNS, "lifts")
    _require_columns(wind, {"timestamp", "mean_ms", "gust_ms"}, "wind")
    zones = zones.copy()
    lifts = lifts.copy()
    wind = wind.copy()
    _numeric(zones, ["x_min", "y_min", "x_max", "y_max", "z_floor", "z_top"], "zones")
    _numeric(lifts, [
        "mass_kg", "area_m2", "cd", "cd_min", "cd_max", "sling_legs",
        "sling_angle_deg", "carrying_legs", "sling_wll_kg", "hook_height_m",
        "radius_m", "tagline_people", "wind_limit_ms", "rope_length_m",
        "tagline_capacity_n", "tagline_stiffness_n_per_m", "crane_x_m", "crane_y_m",
        "slew_angle_deg", "load_length_m", "load_width_m", "load_height_m",
    ], "lifts")
    _numeric(wind, ["mean_ms", "gust_ms"], "wind")
    wind["timestamp"] = pd.to_datetime(wind.timestamp, errors="raise")
    if getattr(wind.timestamp.dt, "tz", None) is not None:
        raise InputError("wind: timestamp должен быть в локальном времени модели без часового пояса")
    if "guardrail" in zones:
        zones["guardrail"] = zones.guardrail.map(_boolean)

    with (directory / "rules.yaml").open(encoding="utf-8") as stream:
        rules = yaml.safe_load(stream)
    with (directory / "building.json").open(encoding="utf-8") as stream:
        building = json.load(stream)
    inputs = Inputs(works, zones, lifts, wind, rules, building, directory)
    _validate(inputs)
    return inputs
