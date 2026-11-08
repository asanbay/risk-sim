from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd
from shapely.geometry import box

COLUMNS = [
    "conflict_id", "rule", "severity", "zone_id", "level", "task_a", "task_b",
    "from", "to", "reference", "details",
]


def overlap(start_a: pd.Timestamp, end_a: pd.Timestamp, start_b: pd.Timestamp, end_b: pd.Timestamp) -> bool:
    return start_a < end_b and start_b < end_a


def _identifier(rule: str, task_a: str, task_b: str, zone: str, start: pd.Timestamp, end: pd.Timestamp) -> str:
    raw = f"{rule}|{task_a}|{task_b}|{zone}|{start.isoformat()}|{end.isoformat()}"
    return "C-" + hashlib.sha1(raw.encode()).hexdigest()[:12].upper()


def _event(
    rule_name: str,
    rules: dict[str, Any],
    zone: pd.Series,
    task_a: str,
    task_b: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    details: dict[str, Any],
) -> dict[str, Any]:
    rule = rules["rules"][rule_name]
    return {
        "conflict_id": _identifier(rule_name, task_a, task_b, zone.zone_id, start, end),
        "rule": rule_name,
        "severity": rule["severity"],
        "zone_id": zone.zone_id,
        "level": zone.level,
        "task_a": task_a,
        "task_b": task_b,
        "from": start,
        "to": end,
        "reference": rule["reference"],
        "details": details,
    }


def _wind_intervals(
    wind: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    metric: str,
    limit: float,
) -> list[dict[str, Any]]:
    inner = wind[(wind.timestamp > start) & (wind.timestamp < end)]
    times = pd.DatetimeIndex([start]).append(pd.DatetimeIndex(inner.timestamp)).append(pd.DatetimeIndex([end]))
    source_times = wind.timestamp.astype("int64").to_numpy(dtype=np.int64)
    source_values = wind[metric].to_numpy(dtype=float)
    values = np.interp(times.astype("int64").to_numpy(dtype=np.int64), source_times, source_values)
    segments: list[dict[str, Any]] = []
    for index in range(len(times) - 1):
        left, right = float(values[index]), float(values[index + 1])
        if left <= limit and right <= limit:
            continue
        t0, t1 = times[index], times[index + 1]
        a, b = t0, t1
        if left <= limit < right:
            fraction = (limit - left) / (right - left)
            a = t0 + (t1 - t0) * fraction
        elif right <= limit < left:
            fraction = (limit - left) / (right - left)
            b = t0 + (t1 - t0) * fraction
        segment_max = max(left, right)
        if segments and a <= segments[-1]["to"]:
            segments[-1]["to"] = max(segments[-1]["to"], b)
            segments[-1]["max_ms"] = max(segments[-1]["max_ms"], segment_max)
        else:
            segments.append({"from": a, "to": b, "max_ms": segment_max})
    return [segment for segment in segments if segment["to"] > segment["from"]]


def detect_conflicts(
    works: pd.DataFrame,
    zones: pd.DataFrame,
    lifts: pd.DataFrame,
    wind: pd.DataFrame,
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    zone_by_id = zones.set_index("zone_id", drop=False)
    polygons = {
        row.zone_id: box(row.x_min, row.y_min, row.x_max, row.y_max)
        for row in zones.itertuples()
    }
    enabled = lambda name: bool(rules["rules"][name].get("enabled", True))

    if enabled("no_two_crews_same_zone"):
        for i in range(len(works)):
            for j in range(i + 1, len(works)):
                a, b = works.iloc[i], works.iloc[j]
                if a.zone_id != b.zone_id or a.crew_id == b.crew_id:
                    continue
                if overlap(a.start, a.end, b.start, b.end):
                    events.append(_event(
                        "no_two_crews_same_zone", rules, zone_by_id.loc[a.zone_id],
                        a.task_id, b.task_id, max(a.start, b.start), min(a.end, b.end),
                        {"crew_a": a.crew_id, "crew_b": b.crew_id, "people_total": int(a.crew_size + b.crew_size)},
                    ))

    if enabled("no_work_under_crane_hazard"):
        crane_tasks = works[works.task_id.isin(lifts.task_id)]
        hazard_ids = set(zones.loc[zones.type == "crane_hazard", "zone_id"])
        seen: set[tuple[str, str, str]] = set()
        for crane in crane_tasks.itertuples():
            if crane.zone_id in hazard_ids:
                crane_hazards = [crane.zone_id]
            else:
                crane_hazards = [zone_id for zone_id in hazard_ids if polygons[zone_id].intersects(polygons[crane.zone_id])]
            for other in works.itertuples():
                if other.task_id == crane.task_id:
                    continue
                if not overlap(crane.start, crane.end, other.start, other.end):
                    continue
                for hazard_id in crane_hazards:
                    key = (crane.task_id, other.task_id, hazard_id)
                    if key in seen or not polygons[hazard_id].intersects(polygons[other.zone_id]):
                        continue
                    seen.add(key)
                    intersection = polygons[hazard_id].intersection(polygons[other.zone_id]).area
                    events.append(_event(
                        "no_work_under_crane_hazard", rules, zone_by_id.loc[hazard_id],
                        crane.task_id, other.task_id, max(crane.start, other.start), min(crane.end, other.end),
                        {"intersection_area_m2": round(intersection, 3), "other_crew": other.crew_id},
                    ))

    if enabled("edge_work_requires_guardrail"):
        for task in works.itertuples():
            tags = set(task.hazard_tags.split(";"))
            zone = zone_by_id.loc[task.zone_id]
            if zone.type != "edge" and "edge" not in tags:
                continue
            guardrail = zone.guardrail if "guardrail" in zone.index else None
            if guardrail is True:
                continue
            status = "not_declared" if guardrail is None or pd.isna(guardrail) else "absent"
            events.append(_event(
                "edge_work_requires_guardrail", rules, zone, task.task_id, "", task.start, task.end,
                {"guardrail_status": status, "requires_field_verification": status == "not_declared"},
            ))

    if enabled("stop_crane_ops_above_wind_limit"):
        metric = rules["rules"]["stop_crane_ops_above_wind_limit"].get("wind_metric", "gust_ms")
        if metric not in {"mean_ms", "gust_ms"}:
            raise ValueError("wind_metric должен быть mean_ms или gust_ms")
        lift_limits = lifts.groupby("task_id").wind_limit_ms.min()
        for task in works[works.task_id.isin(lift_limits.index)].itertuples():
            limit = float(min(task.wind_limit_ms, lift_limits.loc[task.task_id]))
            for interval in _wind_intervals(wind, task.start, task.end, metric, limit):
                events.append(_event(
                    "stop_crane_ops_above_wind_limit", rules, zone_by_id.loc[task.zone_id],
                    task.task_id, "", interval["from"], interval["to"],
                    {"wind_metric": metric, "max_wind_ms": round(interval["max_ms"], 3), "limit_ms": limit},
                ))

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(events, key=lambda event: (event["from"], severity_order.get(event["severity"], 4), event["conflict_id"]))


def conflicts_frame(events: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for event in events:
        row = event.copy()
        row["from"] = row["from"].isoformat()
        row["to"] = row["to"].isoformat()
        row["details"] = json.dumps(row["details"], ensure_ascii=False, sort_keys=True)
        rows.append(row)
    return pd.DataFrame(rows, columns=COLUMNS)


def risk_heatmap(events: list[dict[str, Any]]) -> pd.DataFrame:
    weights = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        start = pd.Timestamp(event["from"])
        iso = start.isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
        key = (str(event["level"]), week)
        row = rows.setdefault(key, {"level": key[0], "week": week, "event_count": 0, "severity_score": 0})
        row["event_count"] += 1
        row["severity_score"] += weights.get(event["severity"], 0)
    return pd.DataFrame(sorted(rows.values(), key=lambda row: (row["level"], row["week"])), columns=["level", "week", "event_count", "severity_score"])
