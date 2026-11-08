"""
core/io/loaders.py — загрузка и нормализация входных данных.

Поддерживаемые форматы (раздел 6 спецификации):
  works.xlsx / works.csv     график работ
  zones.csv                  зоны (work / crane_hazard / edge / passage / storage)
  lifts.csv                  подъёмы из ППР
  wind.csv                   почасовой ветер (mean/gust на высоте измерения)
  crews.csv                  бригады
  crane.yaml                 геометрия и лимиты крана
  crane_load_chart.csv       грузовая характеристика

Принцип: загрузчик ничего не придумывает. Если поле отсутствует — ошибка с
понятным текстом, потому что молчаливое допущение в задачах безопасности
дороже падения скрипта.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import yaml

ZONE_TYPES = {"work", "crane_hazard", "edge", "passage", "storage", "node"}


# ---------------------------------------------------------------------------
# Зоны
# ---------------------------------------------------------------------------
@dataclass
class Zone:
    zone_id: str
    level: str
    type: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    z_floor: float
    z_top: float
    guardrail_installed: bool = False
    note: str = ""

    # -- геометрия -----------------------------------------------------
    @property
    def footprint(self) -> tuple[float, float, float, float]:
        return (self.x_min, self.y_min, self.x_max, self.y_max)

    @property
    def center(self) -> tuple[float, float, float]:
        return (0.5 * (self.x_min + self.x_max), 0.5 * (self.y_min + self.y_max), 0.5 * (self.z_floor + self.z_top))

    @property
    def area_m2(self) -> float:
        return max(0.0, self.x_max - self.x_min) * max(0.0, self.y_max - self.y_min)

    def overlaps_xy(self, other: "Zone") -> float:
        """Площадь пересечения в плане, м2. Без shapely: зоны — прямоугольники."""
        dx = min(self.x_max, other.x_max) - max(self.x_min, other.x_min)
        dy = min(self.y_max, other.y_max) - max(self.y_min, other.y_min)
        return max(0.0, dx) * max(0.0, dy)

    def overlap_fraction(self, other: "Zone") -> float:
        """Доля пересечения от площади меньшей зоны."""
        inter = self.overlaps_xy(other)
        base = min(self.area_m2, other.area_m2)
        return inter / base if base > 1e-9 else 0.0

    def contains_xy(self, x: float, y: float) -> bool:
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max

    def overlaps_z(self, other: "Zone") -> bool:
        return self.z_floor < other.z_top and other.z_floor < self.z_top

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.zone_id, "level": self.level, "type": self.type,
            "x_min": self.x_min, "y_min": self.y_min, "x_max": self.x_max, "y_max": self.y_max,
            "z_floor": self.z_floor, "z_top": self.z_top,
            "guardrail_installed": self.guardrail_installed, "note": self.note,
        }


def load_zones(path: str | Path) -> list[Zone]:
    df = pd.read_csv(path)
    _require(df, ["zone_id", "level", "type", "x_min", "y_min", "x_max", "y_max", "z_floor", "z_top"], path)
    if "guardrail_installed" not in df.columns:
        # Консервативный выбор: если состояние ограждений неизвестно,
        # считаем его ОТСУТСТВУЮЩИМ. Обратное допущение занижает риск.
        print(f"[load_zones] ВНИМАНИЕ: в {Path(path).name} нет колонки guardrail_installed — "
              f"для всех зон принято 'ограждение не установлено' (консервативно).")
        df["guardrail_installed"] = False
    zones: list[Zone] = []
    for _, r in df.iterrows():
        zt = str(r["type"]).strip()
        if zt not in ZONE_TYPES:
            raise ValueError(f"zones.csv: неизвестный тип зоны '{zt}' (зона {r['zone_id']}). "
                             f"Допустимые: {sorted(ZONE_TYPES)}")
        zones.append(Zone(
            zone_id=str(r["zone_id"]).strip(), level=str(r["level"]).strip(), type=zt,
            x_min=float(r["x_min"]), y_min=float(r["y_min"]),
            x_max=float(r["x_max"]), y_max=float(r["y_max"]),
            z_floor=float(r["z_floor"]), z_top=float(r["z_top"]),
            guardrail_installed=_as_bool(r.get("guardrail_installed", False)),
            note=str(r.get("note", "") or ""),
        ))
    ids = [z.zone_id for z in zones]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"zones.csv: дублирующиеся zone_id: {sorted(dupes)}")
    return zones


# ---------------------------------------------------------------------------
# График работ
# ---------------------------------------------------------------------------
@dataclass
class WorkTask:
    task_id: str
    name: str
    zone_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    crew_id: str = ""
    crew_size: int = 0
    equipment_id: str = ""
    hazard_tags: list[str] = field(default_factory=list)
    wind_limit_ms: float = float("inf")
    guardrail_required: bool = False
    installs_guardrail: bool = False
    notes: str = ""

    @property
    def duration_h(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def has_tag(self, tag: str) -> bool:
        return tag in self.hazard_tags

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "name": self.name, "zone_id": self.zone_id,
            "start": self.start.strftime("%Y-%m-%d %H:%M"), "end": self.end.strftime("%Y-%m-%d %H:%M"),
            "crew_id": self.crew_id, "crew_size": self.crew_size, "equipment_id": self.equipment_id,
            "hazard_tags": self.hazard_tags, "wind_limit_ms": (None if math.isinf(self.wind_limit_ms) else self.wind_limit_ms),
            "guardrail_required": self.guardrail_required,
            "installs_guardrail": self.installs_guardrail, "notes": self.notes,
        }


def _split_tags(v: Any) -> list[str]:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return []
    return [t.strip() for t in str(v).replace(",", ";").split(";") if t.strip()]


def load_works(path: str | Path) -> list[WorkTask]:
    p = Path(path)
    if p.suffix.lower() in {".xlsx", ".xlsm"}:
        df = pd.read_excel(p, sheet_name="works")
    elif p.suffix.lower() == ".csv":
        df = pd.read_csv(p)
    else:
        raise ValueError(f"works: неподдерживаемый формат {p.suffix} (ожидается .xlsx или .csv)")

    _require(df, ["task_id", "name", "zone_id", "start", "end"], p)
    df["start"] = pd.to_datetime(df["start"], errors="coerce")
    df["end"] = pd.to_datetime(df["end"], errors="coerce")
    bad = df[df["start"].isna() | df["end"].isna()]
    if len(bad):
        raise ValueError(f"works: не удалось разобрать даты в строках {list(bad.index)}. "
                         f"Формат: '2026-11-16 08:00'.")
    tasks: list[WorkTask] = []
    for _, r in df.iterrows():
        if r["end"] <= r["start"]:
            raise ValueError(f"works: задача {r['task_id']} — конец раньше начала.")
        wlim = r.get("wind_limit_ms", np.nan)
        tasks.append(WorkTask(
            task_id=str(r["task_id"]).strip(), name=str(r["name"]).strip(), zone_id=str(r["zone_id"]).strip(),
            start=r["start"], end=r["end"],
            crew_id=str(r.get("crew_id", "") or "").strip(),
            crew_size=int(r.get("crew_size", 0) or 0),
            equipment_id=str(r.get("equipment_id", "") or "").strip(),
            hazard_tags=_split_tags(r.get("hazard_tags")),
            wind_limit_ms=float(wlim) if pd.notna(wlim) and float(wlim) > 0 else float("inf"),
            guardrail_required=_as_bool(r.get("guardrail_required", False)),
            installs_guardrail=_as_bool(r.get("installs_guardrail", False)),
            notes=str(r.get("notes", "") or ""),
        ))
    return tasks


# ---------------------------------------------------------------------------
# Подъёмы
# ---------------------------------------------------------------------------
@dataclass
class Lift:
    lift_id: str
    task_id: str
    load_name: str
    mass_kg: float
    area_m2: float
    cd_min: float
    cd_max: float
    sling_legs: int
    sling_angle_deg: float
    carrying_legs: int
    sling_wll_kg: float
    hook_height_m: float
    radius_m: float
    pickup: tuple[float, float, float]
    dropoff: tuple[float, float, float]
    tagline_people: int
    wind_limit_ms: float
    daf_mode: str = "normal"

    @property
    def cd_mid(self) -> float:
        return 0.5 * (self.cd_min + self.cd_max)

    @property
    def suspend_length_m(self) -> float:
        """Длина подвеса (крюк -> центр груза) — влияет на период раскачки."""
        return max(1.0, float(self.hook_height_m) - float(self.dropoff[2]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "lift_id": self.lift_id, "task_id": self.task_id, "load_name": self.load_name,
            "mass_kg": self.mass_kg, "area_m2": self.area_m2,
            "cd_min": self.cd_min, "cd_max": self.cd_max,
            "sling_legs": self.sling_legs, "sling_angle_deg": self.sling_angle_deg,
            "carrying_legs": self.carrying_legs, "sling_wll_kg": self.sling_wll_kg,
            "hook_height_m": self.hook_height_m, "radius_m": self.radius_m,
            "pickup": list(self.pickup), "dropoff": list(self.dropoff),
            "tagline_people": self.tagline_people, "wind_limit_ms": self.wind_limit_ms,
            "daf_mode": self.daf_mode, "suspend_length_m": self.suspend_length_m,
        }


def load_lifts(path: str | Path) -> list[Lift]:
    df = pd.read_csv(path)
    _require(df, ["lift_id", "task_id", "mass_kg", "area_m2", "sling_angle_deg",
                  "carrying_legs", "sling_wll_kg", "radius_m", "hook_height_m"], path)
    lifts: list[Lift] = []
    for _, r in df.iterrows():
        cd_min = float(r.get("cd_min", np.nan)) if "cd_min" in df.columns else float("nan")
        cd_max = float(r.get("cd_max", np.nan)) if "cd_max" in df.columns else float("nan")
        if math.isnan(cd_min) or math.isnan(cd_max):
            cd = float(r.get("cd", 1.2))
            cd_min, cd_max = cd, cd
        lifts.append(Lift(
            lift_id=str(r["lift_id"]).strip(), task_id=str(r["task_id"]).strip(),
            load_name=str(r.get("load_name", "") or ""),
            mass_kg=float(r["mass_kg"]), area_m2=float(r["area_m2"]),
            cd_min=cd_min, cd_max=cd_max,
            sling_legs=int(r.get("sling_legs", 4) or 4),
            sling_angle_deg=float(r["sling_angle_deg"]),
            carrying_legs=int(r["carrying_legs"]),
            sling_wll_kg=float(r["sling_wll_kg"]),
            hook_height_m=float(r["hook_height_m"]), radius_m=float(r["radius_m"]),
            pickup=(float(r.get("pickup_x", 0) or 0), float(r.get("pickup_y", 0) or 0), float(r.get("pickup_z", 0) or 0)),
            dropoff=(float(r.get("dropoff_x", 0) or 0), float(r.get("dropoff_y", 0) or 0), float(r.get("dropoff_z", 0) or 0)),
            tagline_people=int(r.get("tagline_people", 0) or 0),
            wind_limit_ms=float(r.get("wind_limit_ms", 0) or 0),
            daf_mode=str(r.get("daf_mode", "normal") or "normal").strip(),
        ))
    for lf in lifts:
        if lf.carrying_legs > lf.sling_legs:
            raise ValueError(f"lifts.csv: {lf.lift_id} — carrying_legs ({lf.carrying_legs}) > sling_legs ({lf.sling_legs})")
        if not (0 < lf.sling_angle_deg <= 90):
            raise ValueError(f"lifts.csv: {lf.lift_id} — угол ветви к горизонтали вне (0, 90]: {lf.sling_angle_deg}")
    return lifts


# ---------------------------------------------------------------------------
# Ветер
# ---------------------------------------------------------------------------
@dataclass
class WindSeries:
    ts: pd.DatetimeIndex
    mean_ms: np.ndarray
    gust_ms: np.ndarray
    ref_height_m: float
    source: str = ""

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame({"timestamp": self.ts, "mean_ms": self.mean_ms,
                             "gust_ms": self.gust_ms, "ref_height_m": self.ref_height_m})


def load_wind(path: str | Path) -> WindSeries:
    df = pd.read_csv(path)
    _require(df, ["timestamp", "mean_ms"], path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    if df["timestamp"].isna().any():
        raise ValueError(f"wind.csv: неразобранные метки времени (строки {list(df.index[df['timestamp'].isna()])})")
    df = df.sort_values("timestamp").reset_index(drop=True)
    if "gust_ms" not in df.columns:
        # честное предупреждение вместо тихого допущения
        print("[load_wind] ВНИМАНИЕ: нет колонки gust_ms — порыв принят равным средней скорости. "
              "Это занижает риск; добавьте данные порывов.")
        df["gust_ms"] = df["mean_ms"]
    ref_h = float(df["ref_height_m"].iloc[0]) if "ref_height_m" in df.columns else 10.0
    src = str(df["source"].iloc[0]) if "source" in df.columns else ""
    return WindSeries(pd.DatetimeIndex(df["timestamp"]), df["mean_ms"].to_numpy(float),
                      df["gust_ms"].to_numpy(float), ref_h, src)


# ---------------------------------------------------------------------------
# Бригады
# ---------------------------------------------------------------------------
@dataclass
class Crew:
    crew_id: str
    name: str
    size: int
    color: str = "#888888"
    home_zone: str = ""
    role: str = ""


def load_crews(path: str | Path) -> list[Crew]:
    df = pd.read_csv(path)
    _require(df, ["crew_id", "name", "size"], path)
    return [Crew(str(r["crew_id"]).strip(), str(r["name"]).strip(), int(r["size"]),
                 str(r.get("color", "#888888")), str(r.get("home_zone", "") or ""), str(r.get("role", "") or ""))
            for _, r in df.iterrows()]


# ---------------------------------------------------------------------------
# Кран
# ---------------------------------------------------------------------------
@dataclass
class CraneSpec:
    id: str
    model: str
    mast_x: float
    mast_y: float
    base_z: float
    top_z: float
    section_m: float
    jib_length_m: float
    counter_jib_m: float
    head_z: float
    trolley_min_r: float
    trolley_max_r: float
    trolley_speed_ms: float
    hoist_speed_ms: float
    max_load_kg: float
    wind_limit_ops_ms: float
    wind_limit_climbing_ms: float
    slew_speed_dps: float
    chart_radius: np.ndarray
    chart_capacity_kg: np.ndarray

    def capacity_kg(self, radius_m: float) -> float:
        """Грузоподъёмность по грузовой характеристике (линейная интерполяция)."""
        r = float(np.clip(radius_m, self.chart_radius[0], self.chart_radius[-1]))
        return float(np.interp(r, self.chart_radius, self.chart_capacity_kg))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "model": self.model,
            "mast": {"x": self.mast_x, "y": self.mast_y, "base_z": self.base_z,
                     "top_z": self.top_z, "section_m": self.section_m},
            "jib": {"length_m": self.jib_length_m, "counter_jib_m": self.counter_jib_m, "head_z": self.head_z},
            "trolley": {"min_radius_m": self.trolley_min_r, "max_radius_m": self.trolley_max_r,
                        "speed_ms": self.trolley_speed_ms},
            "hoist": {"max_speed_ms": self.hoist_speed_ms},
            "limits": {"max_load_kg": self.max_load_kg, "wind_limit_ops_ms": self.wind_limit_ops_ms,
                       "wind_limit_climbing_ms": self.wind_limit_climbing_ms,
                       "slew_speed_dps": self.slew_speed_dps},
            "load_chart": [{"radius_m": float(r), "max_load_kg": float(c)}
                           for r, c in zip(self.chart_radius, self.chart_capacity_kg)],
        }


def load_crane(path: str | Path) -> CraneSpec:
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    c = raw["crane"]
    mast, jib, trolley, hoist, limits = c["mast"], c["jib"], c["trolley"], c["hoist"], c["limits"]

    chart_path = p.parent / c.get("load_chart_csv", "crane_load_chart.csv")
    ch = pd.read_csv(chart_path).sort_values("radius_m")
    radius = ch["radius_m"].to_numpy(float)
    cap = ch["max_load_kg"].to_numpy(float)

    return CraneSpec(
        id=str(c["id"]), model=str(c.get("model", "")),
        mast_x=float(mast["x"]), mast_y=float(mast["y"]),
        base_z=float(mast.get("base_z", 0.0)), top_z=float(mast["top_z"]),
        section_m=float(mast.get("section_m", 2.0)),
        jib_length_m=float(jib["length_m"]), counter_jib_m=float(jib.get("counter_jib_m", 0.0)),
        head_z=float(jib["head_z"]),
        trolley_min_r=float(trolley["min_radius_m"]), trolley_max_r=float(trolley["max_radius_m"]),
        trolley_speed_ms=float(trolley.get("speed_ms", 1.5)),
        hoist_speed_ms=float(hoist.get("max_speed_ms", 1.0)),
        max_load_kg=float(limits["max_load_kg"]),
        wind_limit_ops_ms=float(limits["wind_limit_ops_ms"]),
        wind_limit_climbing_ms=float(limits.get("wind_limit_climbing_ms", limits["wind_limit_ops_ms"])),
        slew_speed_dps=float(limits.get("slew_speed_dps", 0.8)),
        chart_radius=radius, chart_capacity_kg=cap,
    )


# ---------------------------------------------------------------------------
# Набор данных
# ---------------------------------------------------------------------------
@dataclass
class Dataset:
    root: Path
    zones: list[Zone]
    tasks: list[WorkTask]
    lifts: list[Lift]
    wind: WindSeries
    crews: list[Crew]
    crane: CraneSpec

    # -- удобные индексы -------------------------------------------------
    def __post_init__(self) -> None:
        self.zone_by_id: dict[str, Zone] = {z.zone_id: z for z in self.zones}
        self.task_by_id: dict[str, WorkTask] = {t.task_id: t for t in self.tasks}
        self.crew_by_id: dict[str, Crew] = {c.crew_id: c for c in self.crews}

        unknown = sorted({t.zone_id for t in self.tasks} - set(self.zone_by_id))
        if unknown:
            raise ValueError(f"works ссылается на несуществующие зоны: {unknown}. Проверьте zones.csv.")
        unknown_l = sorted({l.task_id for l in self.lifts} - set(self.task_by_id))
        if unknown_l:
            raise ValueError(f"lifts.csv ссылается на несуществующие задачи: {unknown_l}. Проверьте works.")

    def zones_of_type(self, ztype: str) -> list[Zone]:
        return [z for z in self.zones if z.type == ztype]

    def tasks_in_zone(self, zone_id: str) -> list[WorkTask]:
        return [t for t in self.tasks if t.zone_id == zone_id]

    def lifts_for_task(self, task_id: str) -> list[Lift]:
        return [l for l in self.lifts if l.task_id == task_id]

    @property
    def time_span(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        starts = [t.start for t in self.tasks]
        ends = [t.end for t in self.tasks]
        return min(starts), max(ends)


def load_dataset(data_dir: str | Path, works_name: str | None = None) -> Dataset:
    """Загружает весь набор из папки (data/sample по умолчанию)."""
    root = Path(data_dir)
    if not root.exists():
        raise FileNotFoundError(f"Папка данных не найдена: {root}")

    works_path = None
    for cand in ([works_name] if works_name else []) + ["works.xlsx", "works.csv"]:
        p = root / cand
        if p.exists():
            works_path = p
            break
    if works_path is None:
        raise FileNotFoundError(f"В {root} нет ни works.xlsx, ни works.csv")

    return Dataset(
        root=root,
        zones=load_zones(root / "zones.csv"),
        tasks=load_works(works_path),
        lifts=load_lifts(root / "lifts.csv"),
        wind=load_wind(root / "wind.csv"),
        crews=load_crews(root / "crews.csv"),
        crane=load_crane(root / "crane.yaml"),
    )


# ---------------------------------------------------------------------------
# утилиты
# ---------------------------------------------------------------------------
def _require(df: pd.DataFrame, cols: Iterable[str], path: str | Path) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{Path(path).name}: отсутствуют обязательные колонки {missing}. "
                         f"Есть: {list(df.columns)}")


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, float) and math.isnan(v):
        # NaN возникает при ОТСУТСТВУЮЩЕЙ ячейке/колонке. Считать это True
        # нельзя: «ограждение установлено» по умолчанию — опасное допущение.
        return False
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "да", "t"}
