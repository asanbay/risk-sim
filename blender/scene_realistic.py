"""
blender/scene_realistic.py — фотореалистичная сцена стройки ИЗ scene_bundle.json.

Запуск ВНУТРИ Blender (5.x):
    blender -b -P blender/scene_realistic.py -- \
        --bundle results/dynamics/scene_bundle.json \
        --out results/dynamics/crane_scene.blend [--lat 41.0 --lon 29.0 --tz 3]

Отличия от схематичного scene_setup.py:
  * PBR-материалы (бетон, оцинковка, крашеная сталь, арматура, дерево, грунт);
  * башенный кран правильной конструкции: фундамент с балластом, решётчатая
    башня, поворотная платформа, кабина, оголовок с оттяжками, противовес;
  * здание: плиты с колоннами сеткой, ядро со стенами и выпусками арматуры,
    щиты опалубки у ядра, жёлтые ограждения краёв;
  * бригады — фигуры с касками и жилетами цвета бригады;
  * солнце и небо Nishita считаются ПО РЕАЛЬНОМУ времени таймлайна
    (алгоритм NOAA) — тени движутся как в жизни;
  * три камеры: облёт площадки, слежение за крюком, наземная.

Иерархия и имена совместимы с animate_realistic.py и по смыслу со старой
схемой: Crane_Slew / Crane_Trolley / Crane_Hook / Crane_Rope / Load / Crew_* /
Zone_*. Никаких чисел «от себя»: вся геометрия из JSON.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

MAT_COLORS = {
    "work":         (0.28, 0.58, 1.00, 0.22),
    "edge":         (1.00, 0.25, 0.25, 0.38),
    "passage":      (1.00, 0.80, 0.35, 0.18),
    "storage":      (0.62, 0.30, 0.95, 0.18),
    "crane_hazard": (1.00, 0.45, 0.15, 0.12),
}


def log(*a):
    print("[scene_realistic]", *a, flush=True)


def zid(z: dict) -> str:
    return z.get("id") or z.get("zone_id") or "?"


def wipe():
    bpy.ops.wm.read_factory_settings(use_empty=True)


# ---------------------------------------------------------------- материалы

def _finish_mat(m, alpha=1.0):
    try:
        m.blend_method = "BLEND"
    except AttributeError:
        pass
    try:
        m.surface_render_method = "BLENDED"
    except (AttributeError, TypeError):
        pass
    m.use_backface_culling = False
    m.diffuse_color = (*m.diffuse_color[:3], alpha)
    return m


def simple_mat(name, rgba, rough=0.6, metallic=0.0, alpha=1.0, emission=0.0):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    b = nt.nodes.new("ShaderNodeBsdfPrincipled")
    b.inputs["Base Color"].default_value = rgba
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Alpha"].default_value = alpha
    if emission:
        try:
            b.inputs["Emission Color"].default_value = rgba
            b.inputs["Emission Strength"].default_value = emission
        except KeyError:
            pass
    nt.links.new(b.outputs["BSDF"], out.inputs["Surface"])
    return _finish_mat(m, alpha)


def object_color_mat(name, alpha=1.0, rough=0.5):
    """Материал, читающий obj.color: подсветка риска — одним присваиванием."""
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    b = nt.nodes.new("ShaderNodeBsdfPrincipled")
    info = nt.nodes.new("ShaderNodeObjectInfo")
    nt.links.new(info.outputs["Color"], b.inputs["Base Color"])
    b.inputs["Roughness"].default_value = rough
    b.inputs["Alpha"].default_value = alpha
    nt.links.new(b.outputs["BSDF"], out.inputs["Surface"])
    return _finish_mat(m, alpha)


def concrete_mat(name="M_Concrete", base=(0.52, 0.52, 0.50, 1.0)):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    b = nt.nodes.new("ShaderNodeBsdfPrincipled")
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 2.4
    noise.inputs["Detail"].default_value = 8
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.35
    ramp.color_ramp.elements[0].color = (base[0] * 0.75, base[1] * 0.75, base[2] * 0.75, 1)
    ramp.color_ramp.elements[1].position = 0.7
    ramp.color_ramp.elements[1].color = (base[0] * 1.12, base[1] * 1.12, base[2] * 1.1, 1)
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
    b.inputs["Roughness"].default_value = 0.72
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.12
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    nt.links.new(b.outputs["BSDF"], out.inputs["Surface"])
    return m


def soil_mat(name="M_Soil"):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    b = nt.nodes.new("ShaderNodeBsdfPrincipled")
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 0.35
    noise.inputs["Detail"].default_value = 10
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.30, 0.22, 0.14, 1)
    ramp.color_ramp.elements[1].color = (0.50, 0.40, 0.27, 1)
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
    b.inputs["Roughness"].default_value = 0.95
    nt.links.new(b.outputs["BSDF"], out.inputs["Surface"])
    return m


# ---------------------------------------------------------------- примитивы

def add_box(name, x0, y0, z0, x1, y1, z1, mat=None, color=None, collection=None, bevel=0.0):
    cx, cy, cz = (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2
    bpy.ops.mesh.primitive_cube_add(size=1, location=(cx, cy, cz))
    ob = bpy.context.active_object
    ob.name = name
    ob.scale = (max(1e-3, (x1 - x0) / 2), max(1e-3, (y1 - y0) / 2), max(1e-3, (z1 - z0) / 2))
    bpy.ops.object.transform_apply(scale=True)
    # Blender 5.x может запечь позицию в меш: возвращаем origin в геометрический центр,
    # чтобы локальные координаты детей и анимация считались от центра бокса.
    if ob.data.vertices:
        mid = Vector((0.0, 0.0, 0.0))
        for v in ob.data.vertices:
            mid += v.co
        mid /= len(ob.data.vertices)
        if mid.length > 1e-6:
            ob.data.transform(Matrix.Translation(-mid))
    ob.location = (cx, cy, cz)
    if bevel > 0:
        mod = ob.modifiers.new("Bevel", "BEVEL")
        mod.width = bevel
        mod.segments = 2
    if mat:
        ob.data.materials.append(mat)
    if color:
        ob.color = color
    if collection is not None:
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        collection.objects.link(ob)
    return ob


def add_empty(name, loc, parent=None, size=2.0, collection=None, local=False):
    ob = bpy.data.objects.new(name, None)
    ob.empty_display_size = size
    ob.location = loc
    (collection or bpy.context.scene.collection).objects.link(ob)
    if parent is not None:
        ob.parent = parent
        # local=True: координаты заданы в СИСТЕМЕ РОДИТЕЛЯ (детерминированно);
        # иначе сохраняем мировое положение как было.
        ob.matrix_parent_inverse = Matrix.Identity(4) if local else parent.matrix_world.inverted()
    return ob


def attach(child, parent):
    """Родительство без сюрпризов: координаты ребёнка заданы в системе родителя."""
    child.parent = parent
    child.matrix_parent_inverse = Matrix.Identity(4)


def add_rod(name, start, end, radius, mat, collection, verts=8):
    start, end = Vector(start), Vector(end)
    direction = end - start
    if direction.length < 1e-6:
        return None
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius, depth=direction.length,
                                        location=(start + end) / 2)
    ob = bpy.context.active_object
    ob.name = name
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = direction.to_track_quat("Z", "Y")
    ob.data.materials.append(mat)
    for current in list(ob.users_collection):
        current.objects.unlink(ob)
    collection.objects.link(ob)
    return ob


def add_lattice(name, start, end, width, mat, collection, taper=1.0, chord_r=0.11):
    """Решётчатая ферма: 4 пояса, распорки и диагонали; taper<1 сужает к концу."""
    start, end = Vector(start), Vector(end)
    root = add_empty(name, (0, 0, 0), collection=collection)
    length = (end - start).length
    vertical = abs(end.z - start.z) >= abs(end.x - start.x)
    along = Vector((1, 0, 0)) if not vertical else Vector((0, 0, 1))

    def point(fraction, x, y):
        w = width * (1.0 - (1.0 - taper) * fraction) / 2
        if vertical:
            return start + Vector((x * w, y * w, fraction * length))
        return start + Vector((fraction * length, x * w, y * w))

    corners = [(1, 1), (1, -1), (-1, -1), (-1, 1)]
    for index, (x, y) in enumerate(corners):
        ob = add_rod(f"{name}_Chord_{index}", point(0, x, y), point(1, x, y), chord_r, mat, collection)
        if ob:
            ob.parent = root
    rungs = max(2, int(length / 2.5) + 1)
    for index in range(rungs):
        f0 = index / (rungs - 1)
        f1 = (index + 1) / (rungs - 1)
        for side in range(4):
            x0, y0 = corners[side]
            x1, y1 = corners[(side + 1) % 4]
            ob = add_rod(f"{name}_Rung_{index}_{side}", point(f0, x0, y0), point(f0, x1, y1),
                         0.045, mat, collection, verts=6)
            if ob:
                ob.parent = root
        if index < rungs - 1:
            x0, y0 = corners[index % 4]
            x1, y1 = corners[(index + 1) % 4]
            ob = add_rod(f"{name}_Diag_{index}", point(f0, x0, y0), point(f1, x1, y1),
                         0.04, mat, collection, verts=6)
            if ob:
                ob.parent = root
    return root


# ---------------------------------------------------------------- солнце (NOAA)

def sun_direction(dt, lat_deg, lon_deg, tz_hours):
    """Единичный вектор НА солнце для наивного локального времени dt (алгоритм NOAA)."""
    lat = math.radians(lat_deg)
    day = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    g = 2 * math.pi / 365.0 * (day - 1 + (hour - 12) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    time_offset = eqtime + 4 * lon_deg - 60 * tz_hours
    tst = hour * 60 + time_offset
    ha = math.radians(tst / 4.0 - 180.0)
    cos_zen = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    elevation = math.asin(max(-1.0, min(1.0, cos_zen)))
    cos_az = (math.sin(decl) - math.sin(elevation) * math.sin(lat)) / \
             max(1e-6, math.cos(elevation) * math.cos(lat))
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.acos(cos_az)
    if ha > 0:
        az = 2 * math.pi - az          # после полудня — запад
    return elevation, az               # азимут от севера, по часовой


# ---------------------------------------------------------------- сцена

def build_person(name, vest_mat, skin_mat, helmet_mat, collection):
    """Низкополигональная фигура рабочего: сапоги, торс-жилет, голова, каска."""
    parts = []
    legs = add_box(f"{name}_legs", -0.18, -0.12, 0, 0.18, 0.12, 0.85, mat=skin_mat)
    parts.append(legs)
    torso = add_box(f"{name}_torso", -0.26, -0.16, 0.85, 0.26, 0.16, 1.5, mat=vest_mat)
    parts.append(torso)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.14, segments=12, ring_count=8,
                                         location=(0, 0, 1.66))
    head = bpy.context.active_object
    head.name = f"{name}_head"
    head.data.materials.append(skin_mat)
    parts.append(head)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.17, segments=12, ring_count=6,
                                         location=(0, 0, 1.72))
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.transform.resize(value=(1.0, 1.15, 0.55))
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    helmet = bpy.context.active_object
    helmet.name = f"{name}_helmet"
    helmet.data.materials.append(helmet_mat)
    parts.append(helmet)
    for s in (-1, 1):
        arm = add_box(f"{name}_arm{s}", s * 0.26 - 0.07, -0.1, 1.0, s * 0.26 + 0.07, 0.1, 1.45,
                      mat=vest_mat)
        parts.append(arm)
    root = parts[0]
    for ob in parts[1:]:
        ob.parent = root
        ob.matrix_parent_inverse = root.matrix_world.inverted()
    for ob in parts:
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        collection.objects.link(ob)
    root.name = name
    return root


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default="results/dynamics/scene_bundle.json")
    ap.add_argument("--out", default="results/dynamics/crane_scene.blend")
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--lat", type=float, default=41.0, help="широта площадки")
    ap.add_argument("--lon", type=float, default=29.0, help="долгота площадки")
    ap.add_argument("--tz", type=float, default=3.0, help="смещение UTC, ч")
    a = ap.parse_args(argv)

    bundle = json.loads(Path(a.bundle).read_text(encoding="utf-8"))
    tl = bundle["timeline"]
    zones, crane, crews, frames = tl["zones"], tl["crane"], tl["crews"], tl["frames"]

    from datetime import datetime
    t0 = datetime.strptime(tl["meta"]["t_start"], "%Y-%m-%d %H:%M")
    el, az = sun_direction(t0.replace(hour=12, minute=0), a.lat, a.lon, a.tz)
    log(f"проверка солнца: полдень дня старта — высота {math.degrees(el):.1f}°, азимут {math.degrees(az):.1f}°")

    wipe()
    scn = bpy.context.scene
    scn.render.fps = a.fps
    scn.frame_start = 0
    scn.frame_end = max(1, len(frames) - 1)
    scn.frame_set(0)

    # ---------- материалы ----------
    zmats = {k: object_color_mat(f"M_Zone_{k}", alpha=v[3]) for k, v in MAT_COLORS.items()}
    m_slab = concrete_mat("M_Slab", (0.55, 0.55, 0.53, 1))
    m_concrete = concrete_mat("M_Concrete2", (0.60, 0.58, 0.55, 1))
    m_steel_yellow = simple_mat("M_Crane", (0.93, 0.52, 0.05, 1), rough=0.42, metallic=0.15)
    m_steel_galv = simple_mat("M_Galv", (0.62, 0.64, 0.66, 1), rough=0.38, metallic=0.85)
    m_rope = simple_mat("M_Rope", (0.13, 0.12, 0.11, 1), rough=0.55)
    m_rebar = simple_mat("M_Rebar", (0.42, 0.28, 0.18, 1), rough=0.6, metallic=0.9)
    m_guard = simple_mat("M_Guard", (0.90, 0.58, 0.03, 1), rough=0.55)
    m_soil = soil_mat()
    m_stand = concrete_mat("M_Stand", (0.30, 0.30, 0.29, 1))
    m_glass = simple_mat("M_Glass", (0.6, 0.8, 0.9, 0.18), rough=0.08, metallic=0.0, alpha=0.18)
    m_facade = simple_mat("M_Facade", (0.03, 0.06, 0.10, 0.97), rough=0.10, metallic=0.4, alpha=0.97)
    m_wood = simple_mat("M_Wood", (0.47, 0.32, 0.18, 1), rough=0.78)
    m_mesh = simple_mat("M_Fence", (0.65, 0.67, 0.68, 0.35), rough=0.4, metallic=0.8, alpha=0.35)
    m_skin = simple_mat("M_Skin", (0.55, 0.38, 0.28, 1), rough=0.7)
    m_helmet = simple_mat("M_Helmet", (0.95, 0.95, 0.95, 1), rough=0.3)
    m_vest_obj = object_color_mat("M_Vest", alpha=1.0, rough=0.6)
    m_load_default = simple_mat("M_Load", (0.85, 0.42, 0.10, 1), rough=0.5, metallic=0.2)
    m_bucket = simple_mat("M_Bucket", (0.75, 0.25, 0.10, 1), rough=0.5, metallic=0.3)

    # ---------- земля, площадка, ограждение ----------
    xs = [z[k] for z in zones for k in ("x_min", "x_max")]
    ys = [z[k] for z in zones for k in ("y_min", "y_max")]
    site_x0, site_x1 = min(xs) - 22, max(xs) + 22
    site_y0, site_y1 = min(ys) - 22, max(ys) + 22
    add_box("Ground", site_x0 - 120, site_y0 - 120, -0.6, site_x1 + 120, site_y1 + 120, -0.25,
            mat=m_soil)
    add_box("Hardstand", site_x0, site_y0, -0.25, site_x1, site_y1, -0.02, mat=m_stand, bevel=0.04)

    fence_h = 2.4
    n_post = 0
    fx0, fx1, fy0, fy1 = site_x0, site_x1, site_y0, site_y1
    perim = []
    x = fx0
    while x <= fx1:
        perim.append((x, fy0)); perim.append((x, fy1)); x += 6
    y = fy0
    while y <= fy1:
        perim.append((fx0, y)); perim.append((fx1, y)); y += 6
    fence = add_empty("Site_Fence", (0, 0, 0), size=1)
    for i, (px, py) in enumerate(perim):
        add_rod(f"FencePost_{i}", (px, py, -0.02), (px, py, fence_h), 0.04, m_steel_galv, scn.collection).parent = fence
        n_post += 1
    for (ax0, ay0, ax1, ay1) in ((fx0, fy0, fx1, fy0), (fx0, fy1, fx1, fy1),
                                 (fx0, fy0, fx0, fy1), (fx1, fy0, fx1, fy1)):
        mid = ((ax0 + ax1) / 2, (ay0 + ay1) / 2)
        w = ax1 - ax0 or 0.05
        h = ay1 - ay0 or 0.05
        panel = add_box(f"FenceMesh_{mid[0]}_{mid[1]}", ax0, ay0 - h / 2 + (0 if h > 0.1 else 0),
                        0.0, ax1, ay1 + (0 if h > 0.1 else h / 2), fence_h - 0.1, mat=m_mesh)
        panel.parent = fence
    log(f"ограждение: {n_post} стоек")

    # ---------- здание по уровням ----------
    levels: dict[str, list[dict]] = {}
    for z in zones:
        levels.setdefault(z.get("level", "?"), []).append(z)
    build_levels = [lvl for lvl in sorted(levels.keys()) if lvl != "G"]
    if build_levels:
        # footprint строящейся башни
        bx0 = min(z["x_min"] for lvl in build_levels for z in levels[lvl] if z.get("type") != "crane_hazard") - 1.5
        bx1 = max(z["x_max"] for lvl in build_levels for z in levels[lvl] if z.get("type") != "crane_hazard") + 1.5
        by0 = min(z["y_min"] for lvl in build_levels for z in levels[lvl] if z.get("type") != "crane_hazard") - 1.5
        by1 = max(z["y_max"] for lvl in build_levels for z in levels[lvl] if z.get("type") != "crane_hazard") + 1.5
        lowest = min(z["z_floor"] for lvl in build_levels for z in levels[lvl])
        # законченные этажи до земли: плита + тёмное остекление
        floor_h = 4.0
        n_floors = 0
        fz = floor_h
        while fz < lowest - 1.0:
            add_box(f"Floor_Slab_{n_floors:02d}", bx0 + 0.3, by0 + 0.3, fz - 0.35, bx1 - 0.3, by1 - 0.3, fz, mat=m_slab)
            add_box(f"Floor_Facade_{n_floors:02d}", bx0, by0, fz, bx1, by1,
                    fz + floor_h + 0.02, mat=m_facade)
            n_floors += 1
            fz += floor_h
        log(f"законченных этажей: {n_floors} (footprint {bx1-bx0:.0f}×{by1-by0:.0f} м)")
    for lvl, zs in sorted(levels.items()):
        if lvl == "G":
            continue
        z0 = min(z["z_floor"] for z in zs)
        x0 = min(z["x_min"] for z in zs) - 1.5
        x1 = max(z["x_max"] for z in zs) + 1.5
        y0 = min(z["y_min"] for z in zs) - 1.5
        y1 = max(z["y_max"] for z in zs) + 1.5
        add_box(f"Slab_{lvl}", x0, y0, z0 - 0.5, x1, y1, z0, mat=m_slab, bevel=0.05)
        gx = x0 + 3
        while gx <= x1 - 2.5:
            gy = y0 + 3
            while gy <= y1 - 2.5:
                add_box(f"Column_{lvl}_{gx:.0f}_{gy:.0f}", gx - 0.3, gy - 0.3, z0,
                        gx + 0.3, gy + 0.3, z0 + 4.0, mat=m_concrete, bevel=0.04)
                gy += 8
            gx += 8
        log(f"уровень {lvl}: плита {x1-x0:.0f}×{y1-y0:.0f} м")

        # ядро со стенами и выпусками арматуры над верхним ярусом
        core = [z for z in zs if z.get("type") == "work" and "CORE" in zid(z)]
        if core:
            c = core[0]
            is_top = lvl == sorted(levels.keys())[-1]
            t = 0.4
            for name, (wx0, wy0, wx1, wy1) in (
                ("W", (c["x_min"], c["y_min"], c["x_max"], c["y_min"] + t)),
                ("E", (c["x_min"], c["y_max"] - t, c["x_max"], c["y_max"])),
                ("S", (c["x_min"], c["y_min"], c["x_min"] + t, c["y_max"])),
                ("N", (c["x_max"] - t, c["y_min"], c["x_max"], c["y_max"])),
            ):
                add_box(f"Core_{lvl}_{name}", wx0, wy0, c["z_floor"], wx1, wy1,
                        c["z_top"], mat=m_concrete, bevel=0.03)
            if is_top:
                rb_x = c["x_min"] + 0.6
                count = 0
                while rb_x <= c["x_max"] - 0.6:
                    rb_y = c["y_min"] + 0.6
                    while rb_y <= c["y_max"] - 0.6:
                        if min(rb_x - c["x_min"], c["x_max"] - rb_x,
                               rb_y - c["y_min"], c["y_max"] - rb_y) < 1.2:
                            add_rod(f"Rebar_{rb_x:.0f}_{rb_y:.0f}",
                                    (rb_x, rb_y, c["z_top"]), (rb_x, rb_y, c["z_top"] + 1.4),
                                    0.02, m_rebar, scn.collection, verts=6)
                            count += 1
                        rb_y += 0.35
                    rb_x += 0.35
                log(f"выпуски арматуры ядра: {count}")

        # щиты опалубки у рабочих зон верхнего уровня + ограждения краёв
        if lvl == sorted(levels.keys())[-1]:
            for z in zs:
                if z.get("type") == "work" and "CORE" not in zid(z):
                    i = 0
                    px = z["x_min"] + 1.0
                    while px < z["x_max"] - 0.6:
                        add_box(f"Formwork_{zid(z)}_{i}", px, z["y_min"] + 0.2, z["z_floor"],
                                px + 1.2, z["y_min"] + 0.5, z["z_floor"] + 2.6, mat=m_wood)
                        i += 1
                        px += 1.8
                if z.get("type") == "edge":
                    posts = [(z["x_min"], z["y_min"]), ((z["x_min"] + z["x_max"]) / 2, z["y_min"]),
                             (z["x_max"], z["y_min"]), (z["x_max"], (z["y_min"] + z["y_max"]) / 2),
                             (z["x_max"], z["y_max"])]
                    for j, (px, py) in enumerate(posts):
                        add_rod(f"GuardPost_{zid(z)}_{j}", (px, py, z["z_floor"]),
                                (px, py, z["z_floor"] + 1.2), 0.03, m_guard, scn.collection, verts=6)
                    for j in range(len(posts) - 1):
                        add_rod(f"GuardRail_{zid(z)}_{j}_a",
                                (posts[j][0], posts[j][1], z["z_floor"] + 0.5),
                                (posts[j + 1][0], posts[j + 1][1], z["z_floor"] + 0.5),
                                0.025, m_guard, scn.collection, verts=6)
                        add_rod(f"GuardRail_{zid(z)}_{j}_b",
                                (posts[j][0], posts[j][1], z["z_floor"] + 1.1),
                                (posts[j + 1][0], posts[j + 1][1], z["z_floor"] + 1.1),
                                0.025, m_guard, scn.collection, verts=6)

    # ---------- склады на земле ----------
    for z in zones:
        if z.get("type") != "storage" or z.get("level") != "G":
            continue
        for k in range(3):
            bx = z["x_min"] + 1 + k * 3.2
            for layer in range(2):
                add_box(f"Stack_{zid(z)}_{k}_{layer}", bx, z["y_min"] + 1 + layer * 0.15,
                        0.15 * layer, bx + 2.6, z["y_min"] + 2.6 + layer * 0.15,
                        0.18 + 0.18 * layer, mat=m_wood, bevel=0.02)

    # ---------- зоны (полупрозрачные объёмы с подсветкой риска) ----------
    zcoll = bpy.data.collections.new("Zones")
    scn.collection.children.link(zcoll)
    for z in zones:
        h = 1.5 if z.get("type") == "edge" else min(max(0.8, z["z_top"] - z["z_floor"]), 3.0)
        key = z.get("type", "work")
        ob = add_box(f"Zone_{zid(z)}", z["x_min"], z["y_min"], z["z_floor"],
                     z["x_max"], z["y_max"], z["z_floor"] + h,
                     mat=zmats.get(key, zmats["work"]), color=MAT_COLORS.get(key, MAT_COLORS["work"]),
                     collection=zcoll)
        ob["risk_type"] = key
        ob["base_color"] = list(MAT_COLORS.get(key, MAT_COLORS["work"])[:3])
    log(f"зон: {len(zones)}")

    # ---------- кран ----------
    cc = bpy.data.collections.new("Crane")
    scn.collection.children.link(cc)
    mx, my = crane["mast"]["x"], crane["mast"]["y"]
    sec = crane["mast"].get("section_m", 2.0)
    base_z, top = crane["mast"]["base_z"], crane["mast"]["top_z"]
    head_z = crane["jib"].get("head_z", top)
    jl = crane["jib"]["length_m"]
    cjl = crane["jib"].get("counter_jib_m", jl / 3)
    trolley_z = 0.25
    scn["trolley_z"] = trolley_z

    # фундамент и балласт
    add_box("Crane_Foundation", mx - sec, my - sec, -0.02, mx + sec, my + sec, 0.5, mat=m_concrete)
    for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        add_box(f"Crane_Ballast_{sx}_{sy}", mx + sx * sec * 0.8 - 0.9, my + sy * sec * 0.8 - 0.9,
                0.5, mx + sx * sec * 0.8 + 0.9, my + sy * sec * 0.8 + 0.9, 1.7, mat=m_concrete)

    mast = add_lattice("Crane_Mast", (mx, my, 0.5), (mx, my, top), sec, m_steel_yellow, cc)

    # поворотная платформа: все дети — в ЛОКАЛЬНЫХ координатах slew
    slew = add_empty("Crane_Slew", (mx, my, top), parent=mast, size=3.0, collection=cc)
    slew.matrix_parent_inverse = Matrix.Identity(4)

    deck = add_box("Crane_Deck", -2.6, -2.6, -0.5, 2.6, 2.6, 0.6, mat=m_steel_yellow, collection=cc)
    attach(deck, slew)
    cab = add_box("Crane_Cab", 1.2, -2.4, 0.6, 2.5, -1.1, 2.4, mat=m_steel_yellow, collection=cc)
    attach(cab, slew)
    glass = add_box("Crane_CabGlass", 1.25, -2.5, 0.7, 2.45, -2.42, 2.3, mat=m_glass, collection=cc)
    attach(glass, slew)
    machine = add_box("Crane_Machine", -2.4, -1.6, 0.6, -1.0, 1.6, 2.0, mat=m_steel_yellow, collection=cc)
    attach(machine, slew)

    # оголовок с оттяжками: apex над платформой
    apex_z = head_z + 6.0
    apex = add_lattice("Crane_Apex", (0, 0, 0.6), (0, 0, apex_z - top), 1.2, m_steel_yellow, cc, taper=0.35)
    attach(apex, slew)

    jib = add_lattice("Crane_Jib", (0, 0, 0.6), (jl, 0, 0.6), 1.5, m_steel_yellow, cc, taper=0.45, chord_r=0.16)
    attach(jib, slew)
    cjib = add_lattice("Crane_CounterJib", (-cjl, 0, 0.6), (0, 0, 0.6), 1.7, m_steel_yellow, cc)
    attach(cjib, slew)

    # оттяжки: apex -> стрела (40% и 90%), apex -> противовесная консоль
    for frac, tag in ((0.4, "mid"), (0.9, "tip")):
        tie = add_rod(f"Crane_TieJib_{tag}", (0, 0, apex_z - top),
                      (jl * frac, 0, 0.9), 0.05, m_rope, cc)
        attach(tie, slew)
    tie_c = add_rod("Crane_TieCounter", (0, 0, apex_z - top), (-cjl * 0.92, 0, 1.0),
                    0.05, m_rope, cc)
    attach(tie_c, slew)

    # противовес: три бетонные плиты
    for k in range(3):
        cw = add_box(f"Crane_Counterweight_{k}", -cjl + 0.8, -1.6, 0.7,
                     -cjl + 2.4 + k * 1.1, 1.6, 2.6, mat=m_concrete, collection=cc)
        attach(cw, slew)

    # тележка, крюковой блок, трос
    trolley = add_box("Crane_Trolley", -1.3, -1.0, -0.1, 1.3, 1.0, 0.6, mat=m_steel_galv, collection=cc)
    attach(trolley, jib)
    trolley.location.x = 20.0

    hook = add_empty("Crane_Hook", (0, 0, -1.0), parent=trolley, size=1.0, collection=cc, local=True)
    block = add_box("Crane_HookBlock", -0.35, -0.35, -1.9, 0.35, 0.35, -0.9, mat=m_steel_galv, collection=cc)
    attach(block, hook)
    bpy.ops.mesh.primitive_torus_add(major_radius=0.18, minor_radius=0.06, location=(0, 0, -2.15))
    torus = bpy.context.active_object
    torus.name = "Crane_HookTorus"
    torus.data.materials.append(m_steel_galv)
    for c in list(torus.users_collection):
        c.objects.unlink(torus)
    cc.objects.link(torus)
    attach(torus, block)

    # трос: начало ровно под крюком, растяжение вниз масштабом (начало координат сверху)
    rope = add_rod("Crane_Rope", (0, 0, -2.2), (0, 0, -3.2), 0.035, m_rope, cc, verts=6)
    rope.data.transform(Matrix.Translation((0, 0, -0.5)))
    rope.location = (0, 0, -2.2)
    attach(rope, hook)

    # ---------- грузы: тип по названию из ППР ----------
    def build_load_mesh(kind: str):
        if kind == "rebar":
            root = add_empty("Load_Root", (0, 0, 0), size=1)
            for i in range(7):
                add_rod(f"LoadBar_{i}", (0, -0.45 + i * 0.15, -0.05), (12.0, -0.45 + i * 0.15, -0.05),
                        0.035, m_rebar, scn.collection, verts=6).parent = root
            for x in (1.0, 6.0, 11.0):
                add_box(f"LoadStrap_{x}", x - 0.06, -0.55, -0.12, x + 0.06, 0.55, 0.05, mat=m_guard).parent = root
            return root, 12.0, 1.0
        if kind == "bucket":
            root = add_empty("Load_Root", (0, 0, 0), size=1)
            bpy.ops.mesh.primitive_cylinder_add(radius=1.0, depth=1.4, location=(0, 0, -0.8))
            body = bpy.context.active_object
            body.name = "Load_BucketBody"
            body.data.materials.append(m_bucket)
            for c in list(body.users_collection):
                c.objects.unlink(body)
            scn.collection.objects.link(body)
            body.parent = root
            cone = add_box("Load_BucketGate", -0.7, -0.7, -1.9, 0.7, 0.7, -1.5, mat=m_bucket)
            cone.parent = root
            return root, 2.0, 2.0
        if kind == "section":
            root = add_empty("Load_Root", (0, 0, 0), size=1)
            box = add_box("Load_Section", 0, -0.75, -1.5, 3.0, 0.75, 0.0, mat=m_steel_yellow)
            box.parent = root
            for i in range(4):
                add_rod(f"Load_SecChord_{i}", (0, -0.6 + (i % 2) * 1.2, -1.35 + (i // 2) * 1.2),
                        (3.0, -0.6 + (i % 2) * 1.2, -1.35 + (i // 2) * 1.2), 0.05, m_steel_yellow,
                        scn.collection, verts=6).parent = root
            return root, 3.0, 1.5
        # панель опалубки (по умолчанию)
        root = add_empty("Load_Root", (0, 0, 0), size=1)
        panel = add_box("Load_Panel", 0, -0.6, -2.5, 2.5, 0.6, 0.0, mat=m_wood)
        panel.parent = root
        frame = add_box("Load_PanelFrame", -0.06, -0.66, -2.56, 2.56, 0.66, 0.06, mat=m_load_default)
        frame.parent = root
        for i in range(3):
            rib = add_box(f"Load_Rib_{i}", 0.4 + i * 0.8, -0.66, -2.5, 0.5 + i * 0.8, 0.66, 0.0,
                          mat=m_load_default)
            rib.parent = root
        return root, 2.5, 2.5

    def classify_load(name: str) -> str:
        n = name.lower()
        if "арматур" in n:
            return "rebar"
        if "бетон" in n or "бадья" in n:
            return "bucket"
        if "секц" in n:
            return "section"
        return "panel"

    load_names = []
    for lift in bundle.get("lifts", []):
        load_names.append(lift.get("load_name", ""))
    kinds = sorted({classify_load(n) for n in load_names if n}) or ["panel"]
    log(f"типы грузов: {kinds}")

    # ВСЕ типы грузов сразу; аниматор включает нужный по lift_id
    load = add_empty("Load", (mx + 8, my + 8, 2.0), size=1.5, collection=cc)
    load.hide_render = load.hide_viewport = True
    kind_widths: dict[str, float] = {}
    for kind in kinds:
        root, w, _h = build_load_mesh(kind)
        root.name = f"Load_{kind}"
        root.parent = load
        root.location = (-w / 2, 0, 0)
        root.hide_render = root.hide_viewport = True
        kind_widths[kind] = w
    scn["load_kinds"] = ",".join(kinds)
    load_w = max(kind_widths.values())

    # стропы: кольцо над грузом + 4 ветви, Damped Track тянет их к крюку.
    # Высота кольца — из паспортного угла строп (sling_included_angle_deg):
    # tan(α/2) = полуширина / высота кольца.
    first_lift = (bundle.get("lifts") or [{}])[0]
    included_deg = float(((first_lift.get("summary") or {}).get("sling_included_angle_deg")) or 60.0)
    half_span = max(load_w * 0.45, 0.5)
    ring_h = max(1.2, min(half_span / max(0.15, math.tan(math.radians(included_deg / 2.0))), 3.5))
    scn["ring_h"] = ring_h
    ring = add_empty("Load_Ring", (0, 0, ring_h), parent=load, size=0.8, collection=cc, local=True)
    for index, (sx, sy) in enumerate(((-1, -1), (-1, 1), (1, -1), (1, 1))):
        leg = add_rod(f"Sling_{index}", (0, 0, ring_h),
                      (sx * half_span, sy * 0.5, 0.0), 0.03, m_rope, cc, verts=6)
        if leg:
            attach(leg, load)
            con = leg.constraints.new("DAMPED_TRACK")
            con.target = hook
            con.track_axis = "TRACK_Z"
    log(f"строповка: 4 ветви + кольцо (угол {included_deg:.0f}°, кольцо {ring_h:.2f} м)")

    # ---------- ветроуказатель и шкалы ветра (приборы площадки) ----------
    ws_x, ws_y = site_x0 + 4, site_y1 - 4
    add_rod("Windsock_Pole", (ws_x, ws_y, 0), (ws_x, ws_y, 8.0), 0.08, m_steel_galv, scn.collection)
    windsock = add_empty("Windsock", (ws_x, ws_y, 7.8), size=1.0)
    sock = add_box("Windsock_Sock", 0.3, -0.28, -0.28, 3.4, 0.28, 0.28, mat=m_guard)
    attach(sock, windsock)
    # кольца-полосы «флюгера» для читаемости
    for k, frac in enumerate((0.15, 0.45, 0.75)):
        ring_mark = add_box(f"Windsock_Band_{k}", frac * 3.4 - 0.06, -0.30, -0.30,
                            frac * 3.4 + 0.06, 0.30, 0.30, mat=m_steel_galv)
        attach(ring_mark, windsock)

    def add_gauge(name: str, offset_x: float, rgb: tuple, label: str, limit_ms: float):
        gx, gy = ws_x + offset_x, ws_y
        add_rod(f"{name}_Post", (gx, gy, 0), (gx, gy, 0.15), 0.05, m_steel_galv, scn.collection)
        bar = add_box(f"{name}_Bar", gx - 0.22, gy - 0.22, 0, gx + 0.22, gy + 0.22, 1.0,
                      mat=simple_mat(f"M_{name}", (*rgb, 1.0), rough=0.4, emission=1.2))
        # origin в центр -> для роста от земли аниматор двигает и location.z
        limit_mark = add_box(f"{name}_Limit", gx - 0.55, gy - 0.05, limit_ms - 0.05,
                             gx + 0.55, gy + 0.05, limit_ms + 0.05, mat=m_guard)
        limit_mark["limit_ms"] = limit_ms
        bpy.ops.object.text_add(location=(gx - 0.7, gy - 1.2, 0.1))
        txt = bpy.context.active_object
        txt.data.body = label
        txt.data.size = 0.55
        txt.data.align_x = "CENTER"
        txt.rotation_euler.x = math.radians(90)
        txt.data.materials.append(m_guard)
        return bar

    limits = crane.get("limits") or {}
    lim_ops = float(limits.get("wind_limit_ops_ms", 10.0))
    lim_climb = float(limits.get("wind_limit_climbing_ms", 20.0))
    add_gauge("Gauge_Wind10", 3.0, (0.20, 0.85, 0.30), "порыв 10м, м/с", lim_ops)
    add_gauge("Gauge_WindTop", 6.5, (0.95, 0.55, 0.10), "порыв верх, м/с", lim_climb)
    log(f"ветроуказатель + шкалы ветра (пределы {lim_ops}/{lim_climb} м/с)")

    # ---------- лента клиренса: маркеры вдоль пути груза, цвет = габарит ----------
    clr = bundle.get("clearances") or {}
    clr_frames = clr.get("per_frame") or []
    if clr_frames:
        m_ok = simple_mat("M_ClrOK", (0.20, 0.85, 0.30, 0.9), rough=0.4, emission=0.6)
        m_warn = simple_mat("M_ClrWarn", (0.95, 0.70, 0.05, 0.9), rough=0.4, emission=1.2)
        m_bad = simple_mat("M_ClrBad", (1.00, 0.10, 0.08, 0.9), rough=0.4, emission=2.0)
        margin_m = float(clr.get("margin_m", 0.5))
        ccol = bpy.data.collections.new("Clearance")
        scn.collection.children.link(ccol)
        frame_status = {rec["i"]: rec for rec in clr_frames}
        n_markers = 0
        for idx, fr in enumerate(frames):
            rec = frame_status.get(idx)
            if not rec or not fr.get("loads"):
                continue
            status = rec.get("status", "ok")
            mat = m_bad if status == "violation" else (m_warn if status == "warn" else m_ok)
            l0 = fr["loads"][0]
            bpy.ops.mesh.primitive_cube_add(size=1,
                                            location=(float(l0["x"]), float(l0["y"]),
                                                      float(l0["z"]) - 0.2))
            marker = bpy.context.active_object
            marker.name = f"Clearance_{idx:04d}"
            marker.scale = (0.35, 0.35, 0.35)
            marker.data.materials.append(mat)
            for c in list(marker.users_collection):
                c.objects.unlink(marker)
            ccol.objects.link(marker)
            n_markers += 1
        log(f"лента клиренса: {n_markers} маркеров (допуск {margin_m:.2f} м, "
            f"модель: {clr.get('model_file') or 'зоны'})")

    # ---------- бригады ----------
    pcoll = bpy.data.collections.new("Crews")
    scn.collection.children.link(pcoll)
    n_people = 0
    for c in crews:
        hexc = c.get("color", "#e8862a")
        h = str(hexc).lstrip("#")
        rgb = tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)) if len(h) == 6 else (0.91, 0.53, 0.16)
        vest = simple_mat(f"M_Vest_{c['crew_id']}", (*rgb, 1), rough=0.65)
        k = max(1, min(int(c.get("size", 1)), 8))
        for i in range(k):
            ob = build_person(f"Crew_{c['crew_id']}_{i:02d}", vest, m_skin, m_helmet, pcoll)
            ob.location = (mx - 12 + n_people * 1.6, my - 14, 0.0)
            ob.color = (*rgb, 1.0)
            n_people += 1
    log(f"рабочих: {n_people}")

    # ---------- солнце, небо, камеры ----------
    bpy.ops.object.light_add(type="SUN", location=(mx, my, top + 40))
    sun = bpy.context.active_object
    sun.name = "Sun"
    sun.rotation_mode = "QUATERNION"   # иначе rotation_quaternion игнорируется
    sun.data.energy = 35.0
    sun.data.angle = math.radians(0.55)   # физический угловой размер солнца
    elev0, az0 = sun_direction(t0.replace(hour=11, minute=30), a.lat, a.lon, a.tz)
    toward = Vector((math.cos(elev0) * math.sin(az0), math.cos(elev0) * math.cos(az0), math.sin(elev0)))
    sun.rotation_quaternion = (-toward).to_track_quat("-Z", "Y")

    scn.world = bpy.data.worlds.new("World") if not scn.world else scn.world
    scn.world.use_nodes = True
    wnt = scn.world.node_tree
    bg = wnt.nodes.get("Background")
    sky = wnt.nodes.new("ShaderNodeTexSky")
    try:
        try:
            sky.sky_type = "NISHITA"          # Blender 4.x
        except TypeError:
            sky.sky_type = "SINGLE_SCATTERING"  # Blender 5.x: физическая модель
        sky.sun_disc = False
        sky.sun_elevation = elev0
        sky.sun_rotation = az0 - math.pi
        if hasattr(sky, "altitude"):
            sky.altitude = 20
        if hasattr(sky, "dust_density"):
            sky.dust_density = 1.2
    except Exception as exc:
        log(f"физическое небо недоступно: {exc}")
    if bg:
        wnt.links.new(sky.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = 0.55
    scn["sky_node"] = sky.name
    scn["sun_object"] = sun.name
    scn["lat"] = a.lat
    scn["lon"] = a.lon
    scn["tz"] = a.tz

    # фокус и камеры: обзор — вся башня с землёй, слежение — рабочая зона
    top_level = max(float(z["z_floor"]) for z in zones)
    active = [z for z in zones if float(z["z_floor"]) >= top_level - 1 and z.get("type") != "crane_hazard"]
    fx = (min(z["x_min"] for z in active) + max(z["x_max"] for z in active)) / 2
    fy = (min(z["y_min"] for z in active) + max(z["y_max"] for z in active)) / 2
    fz = top_level + 2
    span = max(max(z["x_max"] for z in active) - min(z["x_min"] for z in active),
               max(z["y_max"] for z in active) - min(z["y_min"] for z in active))

    tower_top = max(apex_z, head_z) + 10
    track = add_empty("CamTarget", (fx, fy, tower_top * 0.44), size=1.0)

    def make_camera(name, loc, lens=40):
        bpy.ops.object.camera_add(location=loc)
        cam = bpy.context.active_object
        cam.name = name
        cam.data.lens = lens
        cam.data.clip_end = 6000
        con = cam.constraints.new("TRACK_TO")
        con.target = track
        con.track_axis = "TRACK_NEGATIVE_Z"
        con.up_axis = "UP_Y"
        return cam

    radius = max(320.0, tower_top * 1.8)
    cam_over = make_camera("Cam_Overview", (fx + radius, fy - radius, tower_top * 0.13), lens=30)
    # камера крана: с азимута рабочих поворотов стрелы (~150-190°)
    crane_target = add_empty("CamTarget_Crane", (mx, my, head_z - 32.0), size=1.0)
    bpy.ops.object.camera_add(location=(mx - jl * 1.8, my + jl * 1.1, fz - 95))
    cam_crane = bpy.context.active_object
    cam_crane.name = "Cam_Crane"
    cam_crane.data.lens = 50
    cam_crane.data.clip_end = 6000
    con = cam_crane.constraints.new("TRACK_TO")
    con.target = crane_target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    cam_ground = make_camera("Cam_Ground", (fx - 30, fy - 60, 1.7), lens=28)
    scn.camera = cam_over

    # рендер: Cycles + Metal + denoise
    try:
        scn.render.engine = "CYCLES"
        scn.cycles.samples = 96
        scn.cycles.use_adaptive_sampling = True
        scn.cycles.use_denoising = True
        prefs = bpy.context.preferences.addons.get("cycles")
        if prefs:
            prefs.preferences.compute_device_type = "METAL"
            prefs.preferences.get_devices()
            for d in prefs.preferences.devices:
                d.use = True
            log("devices: " + ", ".join(f"{d.name}[{d.type}]" for d in prefs.preferences.devices))
    except Exception as exc:
        log(f"Cycles/Metal недоступен, останется EEVEE: {exc}")
    try:
        scn.view_settings.view_transform = "AgX"
        for look in ("AgX - Punchy", "Punchy", "Medium High Contrast"):
            try:
                scn.view_settings.look = look
                break
            except TypeError:
                continue
    except Exception:
        pass
    scn.render.resolution_x = 1920
    scn.render.resolution_y = 1080

    # ---------- панель RiskSim внутри файла + инструкция ----------
    panel_src = '''# RiskSim: панель симуляции. Текст с флагом Register — исполняется при
# открытии .blend (если разрешён автозапуск) или вручную (Alt+P).
import bpy

_CLASSES = []


class RISKSIM_PT_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RiskSim"
    bl_label = "Симуляция RiskSim"

    def draw(self, ctx):
        scn = ctx.scene
        col = self.layout.column()
        col.operator("risksim.cycle_camera", text="Следующая камера", icon="CAMERA_DATA")
        col.operator("risksim.next_lift", text="К следующему подъёму", icon="PREVIEW_RANGE")
        col.operator("risksim.apply_view", text="Применить вид/шейдинг", icon="RESTRICT_VIEW_ON")
        col.separator()
        for cname in ("Zones", "Clearance", "Crews"):
            coll = bpy.data.collections.get(cname)
            if coll:
                col.prop(coll, "hide_viewport", text=f"Слой: {cname}")


class RISKSIM_OT_camera(bpy.types.Operator):
    bl_idname = "risksim.cycle_camera"
    bl_label = "RiskSim: следующая камера"

    def execute(self, ctx):
        names = ["Cam_Overview", "Cam_Crane", "Cam_Ground"]
        cur = ctx.scene.camera.name if ctx.scene.camera else ""
        i = (names.index(cur) + 1) % len(names) if cur in names else 0
        ctx.scene.camera = bpy.data.objects[names[i]]
        _force_camera_view(ctx)
        self.report({"INFO"}, f"Камера: {names[i]}")
        return {"FINISHED"}


class RISKSIM_OT_next_lift(bpy.types.Operator):
    bl_idname = "risksim.next_lift"
    bl_label = "RiskSim: к следующему подъёму"

    def execute(self, ctx):
        frames = ctx.scene.get("lift_frames") or []
        if not frames:
            self.report({"WARNING"}, "lift_frames пуст")
            return {"CANCELLED"}
        cur = ctx.scene.frame_current
        nxt = next((f for f in frames if f > cur), frames[0])
        ctx.scene.frame_set(nxt)
        self.report({"INFO"}, f"Кадр {nxt}")
        return {"FINISHED"}


class RISKSIM_OT_view(bpy.types.Operator):
    bl_idname = "risksim.apply_view"
    bl_label = "RiskSim: применить вид/шейдинг"

    def execute(self, ctx):
        _apply_startup_view(ctx)
        return {"FINISHED"}


def _force_camera_view(ctx):
    for win in ctx.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                for sp in area.spaces:
                    if sp.type == "VIEW_3D":
                        sp.region_3d.view_perspective = "CAMERA"


def _apply_startup_view(ctx):
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for sp in area.spaces:
                if sp.type != "VIEW_3D":
                    continue
                sp.shading.type = "MATERIAL"
                sp.shading.use_scene_world = True
                sp.shading.use_scene_lights = True
                sp.overlay.show_floor = False
                sp.overlay.show_cursor = False
                try:
                    if sp.region_3d is not None:
                        sp.region_3d.view_perspective = "CAMERA"
                        sp.region_3d.update()
                except AttributeError:
                    pass
    scn = bpy.context.scene
    scn.frame_set(int(scn.get("open_frame", 200) or 200))
    for win in bpy.context.window_manager.windows:
        win.screen.areas.update()
    bpy.context.view_layer.update()


def _deferred_view():
    """Вид применяется второй раз отложенно: при загрузке файла region_3d
    может быть ещё не готов — таймер гарантирует камерный вид."""
    try:
        _apply_startup_view(bpy.context)
    except Exception as exc:
        print("RiskSim: отложенный вид не применился:", exc)
    return None


_CLASSES.extend([RISKSIM_PT_panel, RISKSIM_OT_camera, RISKSIM_OT_next_lift, RISKSIM_OT_view])


def register():
    for cls in _CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass
    try:
        import bpy.app as _app
        _app.timers.register(_deferred_view, first_interval=0.3)
    except Exception as exc:
        print("RiskSim: таймер недоступен:", exc)
    _apply_startup_view(bpy.context)
    print("RiskSim: панель готова — N-панель во вьюпорте, вкладка RiskSim")


register()
'''
    if "RiskSim_Panel.py" not in bpy.data.texts:
        text = bpy.data.texts.new("RiskSim_Panel.py")
    else:
        text = bpy.data.texts["RiskSim_Panel.py"]
    text.from_string(panel_src)
    text.use_module = True   # Register: исполнится при открытии файла
    howto = bpy.data.texts.new("КАК_ПОЛЬЗОВАТЬСЯ.txt") if "КАК_ПОЛЬЗОВАТЬСЯ.txt" not in bpy.data.texts \
        else bpy.data.texts["КАК_ПОЛЬЗОВАТЬСЯ.txt"]
    howto.from_string(
        "RiskSim — симуляция недели работ (937 кадров = 24 к/с ≈ 39 с).\n\n"
        "1) Если сверху жёлтая плашка «Auto-run disabled» — нажмите Allow Execution:\n"
        "   тогда панель RiskSim и вид применятся сами.\n"
        "2) Иначе: вкладка Scripting → RiskSim_Panel.py → Alt+P (Run Script).\n"
        "3) N-панель «RiskSim» во вьюпорте: камеры, слои, переход к подъёмам.\n"
        "4) Пробел — проиграть неделю; красные маркеры — клиренс < 0.5 м.\n"
        "5) Ветроуказатель и шкалы у ограждения: планки против пределов крана.\n"
        "6) Рендер ролика: blender/render_realistic.sh (Cycles/Metal).\n"
        "7) Точность: blender/validate_scene.py → scene_validation.json (PASS).\n")

    # стартовый вид файла: материальный шейдинг + вид из камеры — через
    # bpy.data.screens, чтобы применилось и при headless-сохранении
    try:
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type != "VIEW_3D":
                    continue
                for space in area.spaces:
                    if space.type != "VIEW_3D":
                        continue
                    space.shading.type = "MATERIAL"
                    space.shading.use_scene_world = True
                    space.shading.use_scene_lights = True
                    space.overlay.show_floor = False
                    if space.region_3d is not None:
                        space.region_3d.view_perspective = "CAMERA"
        over_obj = bpy.data.objects.get("Cam_Overview")
        if over_obj is not None:
            for ob in bpy.context.selected_objects:
                ob.select_set(False)
            over_obj.select_set(True)
            bpy.context.view_layer.objects.active = over_obj
    except Exception as exc:
        log(f"стартовый вид не настроен: {exc}")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out))
    log(f"сцена сохранена: {out} ({len(bpy.data.objects)} объектов)")


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    main(argv)
