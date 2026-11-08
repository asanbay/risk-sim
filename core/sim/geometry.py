"""
core/sim/geometry.py — плоская геометрия зон.

Зоны заданы прямоугольниками (см. zones.csv), поэтому 90 % операций можно
сделать честной арифметикой без зависимостей. Shapely используется только для
коридора движения груза — там нужна буферизация линии. Если shapely не
установлен, включается запасная реализация «коридор = набор прямоугольников»,
чтобы ядро работало на голом Python.

Все координаты — метры в системе координат модели здания (как в IFC/glTF).
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

try:
    from shapely.geometry import LineString, Point, Polygon, box
    from shapely.ops import unary_union
    HAS_SHAPELY = True
except Exception:  # pragma: no cover
    HAS_SHAPELY = False

from core.io.loaders import Zone


# ---------------------------------------------------------------------------
# базовые операции над прямоугольниками
# ---------------------------------------------------------------------------
def zone_polygon(z: Zone):
    if not HAS_SHAPELY:
        raise RuntimeError("shapely не установлен: pip install shapely")
    return box(z.x_min, z.y_min, z.x_max, z.y_max)


def intersection_area(a: Zone, b: Zone) -> float:
    """Площадь пересечения двух зон в плане, м2 (без shapely — быстрее)."""
    dx = min(a.x_max, b.x_max) - max(a.x_min, b.x_min)
    dy = min(a.y_max, b.y_max) - max(a.y_min, b.y_min)
    return max(0.0, dx) * max(0.0, dy)


def overlap_fraction(a: Zone, b: Zone) -> float:
    inter = intersection_area(a, b)
    base = min(a.area_m2, b.area_m2)
    return inter / base if base > 1e-9 else 0.0


def zones_overlap_xy(a: Zone, b: Zone, min_area_m2: float = 0.5) -> bool:
    return intersection_area(a, b) >= min_area_m2


def intersection_center(a: Zone, b: Zone) -> tuple[float, float] | None:
    """Центр прямоугольника пересечения — координата для отчёта и для Blender."""
    x0, x1 = max(a.x_min, b.x_min), min(a.x_max, b.x_max)
    y0, y1 = max(a.y_min, b.y_min), min(a.y_max, b.y_max)
    if x1 <= x0 or y1 <= y0:
        return None
    return (0.5 * (x0 + x1), 0.5 * (y0 + y1))


def zone_at(zones: Iterable[Zone], x: float, y: float, z: float | None = None) -> Zone | None:
    """Зона, содержащая точку. Если z задан — учитывается высотный диапазон."""
    for zz in zones:
        if zz.contains_xy(x, y):
            if z is None or (zz.z_floor - 1e-6 <= z <= zz.z_top + 1e-6):
                return zz
    return None


def distance_point_to_rect(x: float, y: float, z: Zone) -> float:
    """Расстояние от точки до прямоугольника зоны (0, если внутри)."""
    dx = max(z.x_min - x, 0.0, x - z.x_max)
    dy = max(z.y_min - y, 0.0, y - z.y_max)
    return math.hypot(dx, dy)


def distance_point_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def zones_near_point(zones: Iterable[Zone], x: float, y: float, radius_m: float) -> list[tuple[Zone, float]]:
    out = []
    for z in zones:
        d = distance_point_to_rect(x, y, z)
        if d <= radius_m:
            out.append((z, d))
    return sorted(out, key=lambda t: t[1])


# ---------------------------------------------------------------------------
# коридор движения груза
# ---------------------------------------------------------------------------
def corridor_polygon(path: Sequence[tuple[float, float]], width_m: float):
    """Буфер вокруг ломаной траектории груза."""
    if not HAS_SHAPELY:
        raise RuntimeError("shapely не установлен")
    if len(path) < 2:
        return None
    line = LineString(path)
    return line.buffer(width_m / 2.0, cap_style=2, join_style=2)


def zones_crossed_by_path(zones: Iterable[Zone], path: Sequence[tuple[float, float]],
                          width_m: float, z_level: float | None = None) -> list[tuple[Zone, float]]:
    """Зоны, через которые проходит траектория груза. Возвращает (зона, площадь пересечения)."""
    zs = list(zones)
    if HAS_SHAPELY:
        poly = corridor_polygon(path, width_m)
        if poly is None:
            return []
        out = []
        for z in zs:
            if z_level is not None and not (z.z_floor - 1e-6 <= z_level <= z.z_top + 1e-6):
                continue
            inter = poly.intersection(zone_polygon(z))
            if inter.area > 0.25:
                out.append((z, float(inter.area)))
        return out

    # запасной путь без shapely: проверяем расстояние от сегментов до зон
    out = []
    for z in zs:
        if z_level is not None and not (z.z_floor - 1e-6 <= z_level <= z.z_top + 1e-6):
            continue
        dmin = min(distance_point_to_segment(z.center[0], z.center[1], p[0], p[1], q[0], q[1])
                   for p, q in zip(path[:-1], path[1:]))
        if dmin <= width_m / 2.0:
            out.append((z, float("nan")))
    return out


def load_path_polygon(path: Sequence[tuple[float, float]], corridor_width_m: float,
                      end_radius_m: float = 0.0):
    """Динамическая зона опасности ПОД ГРУЗОМ: коридор траектории + круг под
    точкой приёмки.

    Зачем: статическая зона `crane_hazard` «на весь ярус» превращает правило
    «люди под грузом» в постоянный шум — оно срабатывает на всех и всегда,
    и его перестают читать. Опасность конкретна: она там, где в данный
    момент идёт груз. Поэтому зона строится от фактической траектории.
    """
    if not HAS_SHAPELY:
        return None
    from shapely.geometry import Point
    parts = []
    c = corridor_polygon(path, corridor_width_m)
    if c is not None:
        parts.append(c)
    if end_radius_m > 0 and len(path):
        parts.append(Point(path[-1]).buffer(end_radius_m, quad_segs=16))
        parts.append(Point(path[0]).buffer(end_radius_m, quad_segs=16))
    if not parts:
        return None
    return unary_union(parts)


def lift_hazard_polygon(lift, corridor_width_m: float = 8.0, end_radius_m: float = 6.0):
    """Зона опасности для конкретного подъёма из lifts.csv."""
    path = [(lift.pickup[0], lift.pickup[1]), (lift.dropoff[0], lift.dropoff[1])]
    w = max(corridor_width_m, equivalent_diameter_m(lift.area_m2) + 4.0)
    return load_path_polygon(path, w, end_radius_m), w


def point_in_polygon(x: float, y: float, poly) -> bool:
    if poly is None:
        return False
    if not HAS_SHAPELY:
        raise RuntimeError("shapely не установлен")
    from shapely.geometry import Point
    return bool(poly.contains(Point(x, y)) or poly.touches(Point(x, y)))


def distance_to_polygon(x: float, y: float, poly) -> float:
    if poly is None:
        return float("inf")
    from shapely.geometry import Point
    return float(poly.distance(Point(x, y)))


# ---------------------------------------------------------------------------
# служебное
# ---------------------------------------------------------------------------
def bbox_of(zones: Sequence[Zone]) -> tuple[float, float, float, float]:
    return (min(z.x_min for z in zones), min(z.y_min for z in zones),
            max(z.x_max for z in zones), max(z.y_max for z in zones))


def equivalent_diameter_m(area_m2: float) -> float:
    """Диаметр круга равной площади — для оценки габарита груза."""
    return 2.0 * math.sqrt(max(area_m2, 0.0) / math.pi)
