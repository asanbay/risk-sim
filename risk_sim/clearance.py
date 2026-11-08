"""
risk_sim/clearance.py — инженерный клиренс грузового пути против 3D-модели.

Свип траектории: для каждого кадра таймлайна с активным подъёмом берём
  * груз — параллелепипед габаритов вокруг точки (x, y, z-высота..z);
  * строповку — отрезок крюк → верх груза;
  * крюк и трос — точка и отрезок тележка → крюк;
  * стрелу — отрезок ось крана → тележка на высоте jib_head_z;
  * башню — столб на оси крана от земли до оголовка.
Для каждой точки меряем минимальную дистанцию до:
  * меша здания из data/model.glb (импорт IFC) — точно (trimesh) или
    KDTree-оценкой по сэмплированной поверхности (без rtree, с поправкой
    bias = шаг/2, чтобы не завышать габарит);
  * либо до объёмов зон (work/storage) как AABB — процедурный сценарий.

Нарушение: минимальная дистанция < margin_m (по умолчанию 0.5 м, как
требование к безопасному габариту при перемещении грузов near конструкций).
События группируются в Finding-записи (rule_id load_path_clearance), а
покадровые дистанции уходят в scene_bundle["clearances"] для вьюера/Blender.

Чистые функции без pandas-контекста — тестируются на синтетике.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

RULE_ID = "load_path_clearance"
RULE_TITLE = "Габарит грузового пути до конструкции меньше допустимого"
RULE_SOURCE = "ППР: безопасные расстояния при перемещении грузов кранами вблизи зданий и конструкций"

DEFAULT_MARGIN_M = 0.5
SAMPLE_SPACING_M = 0.30          # шаг сэмплирования поверхности меша (KDTree-режим)
MAST_SAMPLE_STEP_M = 8.0
JIB_SAMPLE_STEP_M = 6.0


def classify_load(name: str) -> str:
    """Тип груза по названию из ППР (общая для clearance/usd_stage/Blender)."""
    n = str(name).lower()
    if "арматур" in n:
        return "rebar"
    if "бетон" in n or "бадья" in n:
        return "bucket"
    if "секц" in n:
        return "section"
    return "panel"

# Эвристика половины габаритов груза (м): по названию, иначе из площади (доля 2:1)
_LOAD_DIM_HINTS = {
    "арматур": (6.0, 0.40, 0.40),
    "бетон": (1.0, 1.0, 1.0),
    "бадья": (1.0, 1.0, 1.0),
    "секц": (1.5, 0.75, 0.75),
}
_DEFAULT_LOAD_HEIGHT_M = 2.5


def log(*a):
    print("[clearance]", *a, flush=True)


# ---------------------------------------------------------------------------
# препятствия
# ---------------------------------------------------------------------------
@dataclass
class Obstacles:
    """Меш здания или боксы зон; даёт минимальную дистанцию для массива точек."""
    mode: str = "zones"              # "glb" | "zones"
    triangles: int = 0
    source: str = ""
    _mesh: Any = None                # trimesh.Trimesh (glb, точный режим)
    _tree: Any = None                # scipy cKDTree (glb, приближённый режим)
    _bias_m: float = 0.0
    _boxes: np.ndarray | None = None  # k×6 (zones)
    _box_ids: list[str] = field(default_factory=list)

    def distances(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64)
        if self.mode == "glb":
            return self._dist_mesh(pts)
        return self._dist_boxes(pts)

    def _dist_mesh(self, pts: np.ndarray) -> np.ndarray:
        if self._tree is not None:
            d, _ = self._tree.query(pts, workers=-1)
            return np.maximum(0.0, d - self._bias_m)
        from trimesh.proximity import closest_point
        _, dist, _ = closest_point(self._mesh, pts)
        return np.asarray(dist, dtype=np.float64)

    def _dist_boxes(self, pts: np.ndarray) -> np.ndarray:
        boxes = self._boxes
        lower = boxes[:, None, :3] - pts[None, :, :]
        upper = pts[None, :, :] - boxes[:, None, 3:]
        gap = np.maximum(np.maximum(lower, upper), 0.0)
        return np.sqrt((gap ** 2).sum(axis=2)).min(axis=0)


def obstacles_from_zones(zone_dicts: list[dict]) -> Obstacles:
    boxes, ids = [], []
    for z in zone_dicts or []:
        boxes.append([z["x_min"], z["y_min"], z["z_floor"],
                      z["x_max"], z["y_max"], z["z_top"]])
        ids.append(z.get("id") or z.get("zone_id") or "?")
    if not boxes:
        # препятствий нет — все точки бесконечно далеки
        boxes = [[1e9, 1e9, -1e9, 1e9 + 1, 1e9 + 1, 1e9]]
        ids = ["none"]
    return Obstacles(mode="zones", source="zones", _boxes=np.asarray(boxes, dtype=np.float64),
                     _box_ids=ids)


def obstacles_from_glb(path: str | Path) -> tuple[Obstacles, dict]:
    import trimesh
    from scipy.spatial import cKDTree

    path = Path(path)
    scene = trimesh.load(str(path), force="scene")
    mesh = scene.dump(concatenate=True)
    mesh = mesh.process(validate=True)
    ob = Obstacles(mode="glb", triangles=int(len(mesh.faces)), source=path.name, _mesh=mesh)

    # Точный режим trimesh требует rtree; без него — KDTree по поверхности
    # с поправкой bias = шаг/2, чтобы не завышать габарит.
    try:
        import rtree  # noqa: F401
        use_exact = True
    except ImportError:
        use_exact = False

    if use_exact:
        probe = mesh.sample(min(64, max(8, len(mesh.vertices))))
        t0 = time.perf_counter()
        from trimesh.proximity import closest_point
        closest_point(mesh, probe)
        if time.perf_counter() - t0 < 0.05:
            return ob, {"index": "trimesh_exact", "sample_spacing_m": 0.0}

    count = int(min(300_000, max(20_000, mesh.area / max(SAMPLE_SPACING_M ** 2, 1e-6))))
    # детерминизм: trimesh использует глобальный numpy RNG — фиксируем сид,
    # иначе число событий клиренса гуляет между прогонами
    np.random.seed(42)
    samples, _ = trimesh.sample.sample_surface(mesh, count)
    ob._tree = cKDTree(np.asarray(samples, dtype=np.float64))
    ob._bias_m = SAMPLE_SPACING_M / 2.0
    return ob, {"index": "kdtree", "sample_spacing_m": SAMPLE_SPACING_M, "samples": count}


# ---------------------------------------------------------------------------
# геометрия кадра
# ---------------------------------------------------------------------------
def load_half_dims(lift: dict) -> tuple[float, float, float]:
    """Полугабариты груза (hL, hW, hH): эвристика по названию, иначе из площади."""
    name = str(lift.get("load_name", "")).lower()
    for key, dims in _LOAD_DIM_HINTS.items():
        if key in name:
            return dims
    area = max(0.5, float(lift.get("area_m2") or 3.0))
    w = math.sqrt(area / 2.0)
    return (w, w / 2.0, _DEFAULT_LOAD_HEIGHT_M / 2.0)


def frame_sample_points(frame: dict, crane_meta: dict,
                        dims: tuple[float, float, float]) -> tuple[np.ndarray, list[str]]:
    """Точки свипа одного кадра: башня, стрела, крюк, трос, стропы, груз."""
    loads = frame.get("loads") or []
    cr = frame.get("crane") or {}
    pts: list[tuple[float, float, float]] = []
    labels: list[str] = []

    mast_x = float(cr.get("mast_x", crane_meta["mast"]["x"]))
    mast_y = float(cr.get("mast_y", crane_meta["mast"]["y"]))
    head_z = float(cr.get("jib_head_z_m", crane_meta["mast"]["top_z"]))
    top_z = float(crane_meta["mast"]["top_z"])
    slew = math.radians(float(cr.get("slew_deg", 0.0)))
    radius = float(cr.get("radius_m", 20.0))
    hook_z = float(cr.get("hook_z_m", head_z - 10.0))

    # башня: от земли до оголовка
    z = 2.0
    while z < top_z:
        pts.append((mast_x, mast_y, z))
        labels.append("mast")
        z += MAST_SAMPLE_STEP_M
    pts.append((mast_x, mast_y, top_z))
    labels.append("mast")

    # стрела: ось -> тележка
    trolley = (mast_x + radius * math.cos(slew), mast_y + radius * math.sin(slew))
    n_jib = max(3, int(radius / JIB_SAMPLE_STEP_M))
    for i in range(1, n_jib + 1):
        k = i / n_jib
        pts.append((mast_x + (trolley[0] - mast_x) * k,
                    mast_y + (trolley[1] - mast_y) * k, head_z))
        labels.append("jib")

    # крюк и трос
    pts.append((trolley[0], trolley[1], hook_z))
    labels.append("hook")

    if loads:
        l0 = loads[0]
        lx, ly, lz = float(l0["x"]), float(l0["y"]), float(l0["z"])
        hL, hW, hH = dims
        # груз повёрнут по slew: вращаем его локальные оси вместе со стрелой,
        # иначе длинномер «протыкает» фасад на бумаге (см. вьюер — он вращает)
        slew_rad = math.radians(float(cr.get("slew_deg", 0.0)))
        cs, sn = math.cos(slew_rad), math.sin(slew_rad)
        # трос от тележки до крюковой точки груза
        for k in (0.5,):
            pts.append((trolley[0] + (lx - trolley[0]) * k,
                        trolley[1] + (ly - trolley[1]) * k,
                        hook_z + (lz - hook_z) * k))
            labels.append("rope")
        # верх груза и строповка
        pts.append((lx, ly, lz))
        labels.append("load")
        pts.append((0.5 * (lx + trolley[0]), 0.5 * (ly + trolley[1]), 0.5 * (lz + hook_z)))
        labels.append("sling")
        # рёбра груза (верх и низ), повёрнутые по slew
        for dz in (0.0, -2.0 * hH):
            for sx in (-hL, hL):
                for sy in (-hW, hW):
                    pts.append((lx + sx * cs - sy * sn, ly + sx * sn + sy * cs, lz + dz))
                    labels.append("load")
    return np.asarray(pts, dtype=np.float64), labels


def nearest_zone_id(point: tuple[float, float, float], zone_dicts: list[dict]) -> str:
    best, best_d = "?", float("inf")
    px, py, pz = point
    for z in zone_dicts or []:
        dx = max(z["x_min"] - px, 0.0, px - z["x_max"])
        dy = max(z["y_min"] - py, 0.0, py - z["y_max"])
        dz = max(z["z_floor"] - pz, 0.0, pz - z["z_top"])
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < best_d:
            best, best_d = z.get("id") or z.get("zone_id") or "?", d
    return best


# ---------------------------------------------------------------------------
# расчёт
# ---------------------------------------------------------------------------
@dataclass
class ClearanceResult:
    margin_m: float
    model_file: str | None
    index_mode: str
    obstacle_triangles: int
    per_frame: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    checked_frames: int = 0

    def as_dict(self) -> dict[str, Any]:
        worst = None
        if self.per_frame:
            worst = min(self.per_frame, key=lambda r: r["min_dist_m"])
        return {
            "margin_m": self.margin_m,
            "model_file": self.model_file,
            "index_mode": self.index_mode,
            "obstacle_triangles": self.obstacle_triangles,
            "checked_frames": self.checked_frames,
            "worst": worst,
            "events": self.events,
            "per_frame": self.per_frame,
        }


def compute_clearance(frames: list[dict], crane_meta: dict,
                      lifts: dict[str, dict], margin_m: float = DEFAULT_MARGIN_M,
                      model_path: str | Path | None = None,
                      zones: list[dict] | None = None,
                      frame_step: int = 1) -> ClearanceResult:
    """Свип по кадрам таймлайна. model_path имеет приоритет над зонами."""
    if model_path is not None:
        obstacles, idx_info = obstacles_from_glb(model_path)
        log(f"меш {obstacles.source}: {obstacles.triangles} треуг., индекс {idx_info['index']}")
    else:
        obstacles = obstacles_from_zones(zones or [])
        idx_info = {"index": "zones_aabb"}
        log(f"меш недоступен — препятствия по зонам: {len(obstacles._box_ids)} боксов")

    result = ClearanceResult(margin_m=margin_m,
                             model_file=obstacles.source if obstacles.mode == "glb" else None,
                             index_mode=str(idx_info["index"]),
                             obstacle_triangles=obstacles.triangles)
    dims_cache: dict[str, tuple[float, float, float]] = {}
    warn_limit = 2.0 * margin_m

    for i in range(0, len(frames), max(1, frame_step)):
        frame = frames[i]
        loads = frame.get("loads") or []
        if not loads or not frame.get("crane"):
            continue
        l0 = loads[0]
        lid = l0.get("lift_id", "?")
        dims = dims_cache.setdefault(lid, load_half_dims(lifts.get(lid, l0)))
        pts, labels = frame_sample_points(frame, crane_meta, dims)
        dists = obstacles.distances(pts)
        j = int(np.argmin(dists))
        min_dist = float(dists[j])
        status = "violation" if min_dist < margin_m else ("warn" if min_dist < warn_limit else "ok")
        result.per_frame.append({
            "i": i,
            "t": frame.get("t"),
            "lift_id": lid,
            "min_dist_m": round(min_dist, 3),
            "component": labels[j] if j < len(labels) else "?",
            "point": [round(float(v), 2) for v in pts[j]],
            "status": status,
        })
        result.checked_frames += 1

    result.events = group_events(result.per_frame, margin_m)
    if result.events:
        log(f"нарушений габарита: {len(result.events)} событий "
            f"(худшая дистанция {min((e['min_dist_m'] for e in result.events), default=0):.2f} м)")
    return result


def group_events(per_frame: list[dict], margin_m: float) -> list[dict]:
    """Сливает идущие подряд кадры нарушения в события с худшей точкой."""
    events: list[dict] = []
    current: list[dict] = []
    for rec in per_frame:
        if rec["status"] == "violation":
            current.append(rec)
        elif current:
            events.append(_finalize_event(current, margin_m))
            current = []
    if current:
        events.append(_finalize_event(current, margin_m))
    return events


def _finalize_event(frames_rec: list[dict], margin_m: float) -> dict:
    worst = min(frames_rec, key=lambda r: r["min_dist_m"])
    return {
        "start": frames_rec[0]["t"],
        "end": frames_rec[-1]["t"],
        "lift_id": worst.get("lift_id"),
        "min_dist_m": worst["min_dist_m"],
        "margin_m": margin_m,
        "component": worst["component"],
        "point": worst["point"],
        "frames": len(frames_rec),
        "status": "critical" if worst["min_dist_m"] < 0.5 * margin_m else "high",
    }


def events_to_findings(clearance_res: "ClearanceResult", ds) -> list[Any]:
    """События клиренса → Finding (rule_id load_path_clearance). Нужен ds зон/задач."""
    from core.sim.rules import Finding
    import pandas as pd

    zone_dicts = [z.to_dict() if hasattr(z, "to_dict") else dict(vars(z)) for z in ds.zones]
    lift_by_id = {lf.lift_id: lf for lf in ds.lifts}
    out: list[Any] = []
    for ev in clearance_res.events:
        lift = lift_by_id.get(ev.get("lift_id"))
        task_ids = [lift.task_id] if lift is not None else []
        point = tuple(ev.get("point") or (0.0, 0.0, 0.0))
        message = (f"Подъём {ev.get('lift_id')}: минимальная дистанция грузового пути "
                   f"до конструкции {ev['min_dist_m']:.2f} м < допуска {ev['margin_m']:.2f} м "
                   f"(элемент: {ev['component']}, точка x={point[0]:.1f}, y={point[1]:.1f}, z={point[2]:.1f}).")
        out.append(Finding(
            rule_id=RULE_ID,
            title=RULE_TITLE,
            severity=ev["status"],
            source=RULE_SOURCE,
            message=message,
            start=pd.Timestamp(ev["start"]),
            end=pd.Timestamp(ev["end"]),
            zone_ids=[nearest_zone_id(point, zone_dicts)],
            task_ids=task_ids,
            people_exposed=0,
            location=point,
            metrics={"min_distance_m": ev["min_dist_m"], "margin_m": ev["margin_m"],
                     "component": ev["component"], "model": clearance_res.model_file or "zones",
                     "index": clearance_res.index_mode},
            lift_id=ev.get("lift_id"),
        ))
    return out
