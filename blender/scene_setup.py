"""
blender/scene_setup.py — сборка базовой сцены .blend ИЗ results/pilot/scene_bundle.json.

Запускается ВНУТРИ Blender один раз (Blender 4.2+ / 5.x):
    blender -b -P blender/scene_setup.py -- \
        --bundle results/pilot/scene_bundle.json --out results/pilot/building.blend

Принцип (раздел 3 ТЗ): Blender не знает ничего о расчётах. Он читает тот же
JSON, что и браузер, и строит параметрическую сцену с понятной иерархией имён
(раздел 9.1): Slab_L62, Core, Zone_*, Crane_*, Crew_*, Load.

Что строится:
  * плиты перекрытий по уровням зон + ядро;
  * зоны как полупрозрачные объёмы с материалом, читающим Object Info -> Color,
    чтобы подсветка риска делалась одним присваиванием obj.color (без нод);
  * кран: башня, оголовок, поворотная часть (пустышка Crane_Slew), стрела,
    противовес, тележка, крюк-пустышка, трос — иерархией родитель -> потомок,
    чтобы animate.py двигал только три параметра: поворот, вылет, высоту;
  * груз (Load) — отдельный объект в мировых координатах: его позицию берём
    прямо из JSON, поэтому расхождение «анимация vs расчёт» исключено;
  * бригады-капсулы (по числу людей, не больше 10 на бригаду).

Это СХЕМА, а не красота. Красота (текстуры, Mixamo, свет, EEVEE/Cycles)
подключается поверх, не трогая иерархию и имена.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

MAT_COLORS = {
    "work":         (0.30, 0.64, 1.00, 0.22),
    "edge":         (1.00, 0.30, 0.31, 0.30),
    "passage":      (1.00, 0.82, 0.40, 0.20),
    "storage":      (0.66, 0.33, 0.96, 0.22),
    "crane_hazard": (1.00, 0.55, 0.26, 0.10),
    "node":         (0.49, 0.91, 0.53, 0.15),
}
HIGHLIGHT = (1.00, 0.10, 0.10, 1.0)


def log(*a):
    print("[scene_setup]", *a, flush=True)


def zid(z: dict) -> str:
    """Ключ id зоны: в бандле 'id', но держим совместимость со старым 'zone_id'."""
    return z.get("id") or z.get("zone_id") or "?"


def work_focus(zones):
    level = max(float(z["z_floor"]) for z in zones)
    active = [z for z in zones if float(z["z_floor"]) >= level - 1 and z.get("type") != "crane_hazard"]
    x_min = min(float(z["x_min"]) for z in active)
    x_max = max(float(z["x_max"]) for z in active)
    y_min = min(float(z["y_min"]) for z in active)
    y_max = max(float(z["y_max"]) for z in active)
    return (x_min + x_max) / 2, (y_min + y_max) / 2, level + 2, max(x_max - x_min, y_max - y_min)


def wipe():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_mat(name, rgba, *, emission=0.0, from_object_color=False):
    """Материал; если from_object_color — базовый цвет берётся из obj.color."""
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    if from_object_color:
        info = nt.nodes.new("ShaderNodeObjectInfo")
        nt.links.new(info.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = rgba
    bsdf.inputs["Alpha"].default_value = rgba[3]
    bsdf.inputs["Roughness"].default_value = 0.65
    if emission:
        try:
            bsdf.inputs["Emission Color"].default_value = rgba
            bsdf.inputs["Emission Strength"].default_value = emission
        except KeyError:  # старые версии Blender
            pass
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    # прозрачность: API менялся между 4.1 и 4.2 — пробуем оба варианта
    try:
        m.blend_method = "BLEND"          # <= 4.1
    except AttributeError:
        pass
    try:
        m.surface_render_method = "BLENDED"   # >= 4.2
    except (AttributeError, TypeError):
        pass
    m.use_backface_culling = False
    m.diffuse_color = rgba
    return m


def add_box(name, x0, y0, z0, x1, y1, z1, mat=None, color=None, collection=None):
    bpy.ops.mesh.primitive_cube_add(size=1, location=((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))
    ob = bpy.context.active_object
    ob.name = name
    ob.scale = (max(1e-3, (x1 - x0) / 2), max(1e-3, (y1 - y0) / 2), max(1e-3, (z1 - z0) / 2))
    bpy.ops.object.transform_apply(scale=True)
    if mat:
        ob.data.materials.append(mat)
    if color:
        ob.color = color
    if collection is not None:
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        collection.objects.link(ob)
    return ob


def add_empty(name, loc, parent=None, size=2.0, collection=None):
    ob = bpy.data.objects.new(name, None)
    ob.empty_display_size = size
    ob.location = loc
    (collection or bpy.context.scene.collection).objects.link(ob)
    if parent is not None:
        ob.parent = parent
        ob.matrix_parent_inverse = parent.matrix_world.inverted()
    return ob


def add_rod(name, start, end, radius, mat, collection):
    start, end = Vector(start), Vector(end)
    direction = end - start
    if direction.length < 1e-6:
        return None
    bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=radius, depth=direction.length, location=(start + end) / 2)
    ob = bpy.context.active_object
    ob.name = name
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = direction.to_track_quat("Z", "Y")
    ob.data.materials.append(mat)
    for current in list(ob.users_collection):
        current.objects.unlink(ob)
    collection.objects.link(ob)
    return ob


def add_lattice(name, start, end, width, mat, collection):
    start, end = Vector(start), Vector(end)
    root = add_empty(name, (0, 0, 0), collection=collection)
    root.empty_display_size = 1.0
    length = (end - start).length
    vertical = abs(end.z - start.z) >= abs(end.x - start.x)
    corners = [(x, y) for x in (-width / 2, width / 2) for y in (-width / 2, width / 2)]

    def point(fraction, x, y):
        if vertical:
            return start + Vector((x, y, fraction * length))
        return start + Vector((fraction * length, x, y))

    for index, (x, y) in enumerate(corners):
        ob = add_rod(f"{name}_Chord_{index}", point(0, x, y), point(1, x, y), 0.075, mat, collection)
        ob.parent = root
    rungs = max(2, int(length / 2.5) + 1)
    for index in range(rungs):
        fraction = index / (rungs - 1)
        for side in range(4):
            x0, y0 = corners[side]
            x1, y1 = corners[(side + 1) % 4]
            ob = add_rod(f"{name}_Rung_{index}_{side}", point(fraction, x0, y0), point(fraction, x1, y1), 0.045, mat, collection)
            ob.parent = root
        if index < rungs - 1:
            x0, y0 = corners[index % 4]
            x1, y1 = corners[(index + 1) % 4]
            ob = add_rod(f"{name}_Diagonal_{index}", point(fraction, x0, y0), point((index + 1) / (rungs - 1), x1, y1), 0.035, mat, collection)
            ob.parent = root
    return root


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default="results/pilot/scene_bundle.json")
    ap.add_argument("--out", default="results/pilot/building.blend")
    ap.add_argument("--fps", type=int, default=24)
    a = ap.parse_args(argv)

    bundle = json.loads(Path(a.bundle).read_text(encoding="utf-8"))
    tl = bundle["timeline"]
    zones, crane, crews, frames = tl["zones"], tl["crane"], tl["crews"], tl["frames"]

    wipe()
    scn = bpy.context.scene
    scn.render.fps = a.fps
    scn.frame_start = 0
    scn.frame_end = max(1, len(frames) - 1)
    scn.render.resolution_x = 1280
    scn.render.resolution_y = 720
    if hasattr(scn.render.image_settings, "media_type"):
        scn.render.image_settings.media_type = "VIDEO"
    scn.render.image_settings.file_format = "FFMPEG"
    scn.render.ffmpeg.format = "MPEG4"
    scn.render.ffmpeg.codec = "H264"
    scn.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scn.render.filepath = str(Path(a.out).parent / "sim.mp4")
    scn.frame_set(0)

    # ---------- материалы ----------
    zmats = {k: make_mat(f"M_{k}", v, from_object_color=True) for k, v in MAT_COLORS.items()}
    m_slab = make_mat("M_slab", (0.14, 0.16, 0.20, 1.0))
    m_steel = make_mat("M_crane", (1.00, 0.62, 0.05, 1.0))
    m_load = make_mat("M_load", (0.91, 0.46, 0.23, 1.0), emission=0.2)
    m_glass = make_mat("M_glass", (0.55, 0.80, 1.00, 0.08))

    # ---------- модель здания или процедурная оболочка ----------
    model_info = bundle.get("model")
    if model_info:
        model_path = Path(a.bundle).parent / model_info["glb_file"]
        if not model_path.is_file():
            raise FileNotFoundError(f"Не найдена импортированная IFC/GLB-модель: {model_path}")
        before_import = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(model_path))
        imported = set(bpy.data.objects) - before_import
        model_root = add_empty("IFC_Model", (0, 0, 0))
        for ob in imported:
            if ob.parent is None:
                ob.parent = model_root
        if model_info.get("glb_coordinates") == "ifc_coordinates_preserved":
            model_root.rotation_euler.x = -math.pi / 2
        scn["ifc_source_sha256"] = model_info["source_sha256"]
        scn["ifc_model_file"] = model_info["source_file"]
        bpy.context.view_layer.update()
        cutaway_z = work_focus(zones)[2] + 6
        hidden = 0
        for ob in imported:
            if ob.type != "MESH":
                continue
            min_z = min((ob.matrix_world @ Vector(corner)).z for corner in ob.bound_box)
            if min_z > cutaway_z:
                ob.hide_render = ob.hide_viewport = True
                hidden += 1
        log(f"IFC/GLB: {model_path.name}; объектов: {len(imported)}; скрыто разрезом: {hidden}")
    else:
        levels: dict[str, list[dict]] = {}
        for z in zones:
            levels.setdefault(z.get("level", "?"), []).append(z)
        for lvl, zs in sorted(levels.items()):
            if lvl == "G":
                continue
            z0 = min(z["z_floor"] for z in zs)
            x0 = min(z["x_min"] for z in zs) - 2
            x1 = max(z["x_max"] for z in zs) + 2
            y0 = min(z["y_min"] for z in zs) - 2
            y1 = max(z["y_max"] for z in zs) + 2
            add_box(f"Slab_{lvl}", x0, y0, z0 - 0.45, x1, y1, z0, mat=m_slab)
            for x in (x0 + 2, x1 - 2):
                for y in (y0 + 2, y1 - 2):
                    add_box(f"Column_{lvl}_{x}_{y}", x - 0.25, y - 0.25, z0, x + 0.25, y + 0.25, z0 + 4.0, mat=m_slab)
        log(f"плит уровней: {len(levels)}")

        core = [z for z in zones if z.get("type") == "work" and "CORE" in zid(z)]
        if core:
            c = max(core, key=lambda z: z["z_floor"])
            ztop = max(z["z_top"] for z in zones)
            add_box("Core", c["x_min"], c["y_min"], c["z_floor"] - 4,
                    c["x_max"], c["y_max"], ztop + 6, mat=m_slab)
            add_box("Core_glass", c["x_min"] - 0.5, c["y_min"] - 0.5, ztop,
                    c["x_max"] + 0.5, c["y_max"] + 0.5, ztop + 6, mat=m_glass)

    add_box("Ground", -30, -30, -0.25, 110, 110, -0.05,
            mat=make_mat("M_ground", (0.06, 0.075, 0.09, 1.0)))

    # ---------- зоны ----------
    zcoll = bpy.data.collections.new("Zones")
    scn.collection.children.link(zcoll)
    for z in zones:
        h = 1.5 if z.get("type") == "edge" else min(max(0.6, z["z_top"] - z["z_floor"]), 3.0)
        ob = add_box(f"Zone_{zid(z)}", z["x_min"], z["y_min"], z["z_floor"],
                     z["x_max"], z["y_max"], z["z_floor"] + h,
                     mat=zmats.get(z.get("type", "work"), zmats["work"]),
                     color=MAT_COLORS.get(z.get("type", "work"), MAT_COLORS["work"]),
                     collection=zcoll)
        ob["risk_type"] = z.get("type", "work")
        ob["base_color"] = list(MAT_COLORS.get(z.get("type", "work"), MAT_COLORS["work"])[:3])
    log(f"зон: {len(zones)}")

    # ---------- кран ----------
    cc = bpy.data.collections.new("Crane")
    scn.collection.children.link(cc)
    mx, my = crane["mast"]["x"], crane["mast"]["y"]
    s = crane["mast"]["section_m"] / 2
    base_z, top = crane["mast"]["base_z"], crane["mast"]["top_z"]
    head_z = crane["jib"].get("head_z", top - 4)
    jl = crane["jib"]["length_m"]
    cjl = crane["jib"].get("counter_jib_m", jl / 3)

    m_tire = make_mat("M_tire", (0.025, 0.025, 0.03, 1.0))
    chassis = add_box("Crane_Chassis", mx - 3, my - 2, 0.4, mx + 3, my + 2, 1.4,
                      mat=m_steel, collection=cc)
    for x in (mx - 2, mx + 2):
        for y in (my - 1.5, my + 1.5):
            add_rod(f"Wheel_{x}_{y}", (x, y - 0.7, 0.65), (x, y + 0.7, 0.65), 0.55, m_tire, cc)
    for sx in (-1, 1):
        for sy in (-1, 1):
            add_rod(f"Outrigger_{sx}_{sy}", (mx + sx * 2.2, my + sy * 1.4, 1.0),
                    (mx + sx * 4.2, my + sy * 3.2, 0.15), 0.09, m_steel, cc)
            add_box(f"OutriggerPad_{sx}_{sy}", mx + sx * 4.2 - 0.55, my + sy * 3.2 - 0.55, 0.0,
                    mx + sx * 4.2 + 0.55, my + sy * 3.2 + 0.55, 0.18, mat=m_steel, collection=cc)

    mast = add_lattice("Crane_Mast", (mx, my, base_z), (mx, my, top), max(1.2, s * 2), m_steel, cc)
    add_box("Crane_Head", mx - 2.5, my - 2.5, top, mx + 2.5, my + 2.5, top + 9,
            mat=m_steel, collection=cc)

    slew = add_empty("Crane_Slew", (mx, my, top + 4), parent=mast, size=3.0, collection=cc)
    jib = add_lattice("Crane_Jib", (0, 0, 0), (jl, 0, 0), 1.6, m_steel, cc)
    jib.parent = slew
    jib.matrix_parent_inverse = slew.matrix_world.inverted()
    cjib = add_lattice("Crane_CounterJib", (-cjl, 0, 0), (0, 0, 0), 1.8, m_steel, cc)
    cjib.parent = slew
    cjib.matrix_parent_inverse = slew.matrix_world.inverted()
    cw = add_box("Crane_Counterweight", -cjl - 2.5, -2.4, -3.4, -cjl, 2.4, 0.6,
                 mat=m_steel, collection=cc)
    cw.parent = cjib
    cw.matrix_parent_inverse = cjib.matrix_world.inverted()

    trolley = add_box("Crane_Trolley", -1.4, -1.1, -1.6, 1.4, 1.1, 0.0, mat=m_steel, collection=cc)
    trolley.parent = jib
    trolley.matrix_parent_inverse = jib.matrix_world.inverted()
    trolley.location.x = 20.0

    hook = add_empty("Crane_Hook", (0, 0, -1.6), parent=trolley, size=1.2, collection=cc)
    rope = add_box("Crane_Rope", -0.07, -0.07, -1.0, 0.07, 0.07, 0.0, mat=m_steel, collection=cc)
    rope.parent = hook
    rope.matrix_parent_inverse = hook.matrix_world.inverted()

    # груз — отдельный объект в МИРОВЫХ координатах: позиция берётся из JSON
    load = add_box("Load", -1.6, -1.0, -2.0, 1.6, 1.0, 0.0, mat=m_load, collection=cc)
    load.hide_render = load.hide_viewport = True   # покажется в кадре первого подъёма
    for index, (sx, sy) in enumerate(((-1.2, -0.8), (-1.2, 0.8), (1.2, -0.8), (1.2, 0.8))):
        sling = add_rod(f"Sling_{index}", (0, 0, 1.8), (sx, sy, 0.0), 0.035, m_steel, cc)
        sling.parent = load
        sling.matrix_parent_inverse = load.matrix_world.inverted()
    for index, x in enumerate((-1.2, -0.6, 0.0, 0.6, 1.2)):
        rib = add_box(f"LoadRib_{index}", x - 0.035, -0.95, -0.12, x + 0.035, 0.95, 0.12, mat=m_load, collection=cc)
        rib.parent = load
        rib.matrix_parent_inverse = load.matrix_world.inverted()

    # ---------- бригады-капсулы ----------
    pcoll = bpy.data.collections.new("Crews")
    scn.collection.children.link(pcoll)
    n_people = 0
    for c in crews:
        cm = make_mat(f"M_crew_{c['crew_id']}", _hex_rgba(c.get("color", "#7ee787"), 1.0))
        k = max(1, min(int(c.get("size", 1)), 10))
        for i in range(k):
            bpy.ops.mesh.primitive_cylinder_add(radius=0.35, depth=1.8,
                                                location=(mx + 5 + i, my + 5, base_z + 1))
            ob = bpy.context.active_object
            ob.name = f"Crew_{c['crew_id']}_{i:02d}"
            ob.data.materials.append(cm)
            for col in list(ob.users_collection):
                col.objects.unlink(ob)
            pcoll.objects.link(ob)
            n_people += 1
    log(f"капсул бригад: {n_people}")

    # ---------- свет и камера ----------
    bpy.ops.object.light_add(type="SUN", location=(mx + 90, my - 120, top + 60))
    sun = bpy.context.active_object
    sun.name = "Sun"
    sun.data.energy = 3.2
    sun.data.angle = math.radians(12)
    sun.rotation_euler = (math.radians(52), math.radians(8), math.radians(35))

    bpy.ops.object.light_add(type="AREA", location=(mx - 60, my + 60, top - 30))
    fill = bpy.context.active_object
    fill.name = "Fill"
    fill.data.energy = 4000
    fill.data.size = 120

    fx, fy, fz, span = work_focus(zones)
    focus_lift = max(0.0, (top - fz) * 0.35)
    target_z = fz + focus_lift
    distance = max(70.0, span * 1.4) * (1.25 if focus_lift > 10 else 1.0)
    bpy.ops.object.camera_add(location=(fx + distance, fy - distance, target_z + distance * 0.55))
    cam = bpy.context.active_object
    cam.name = "Camera"
    cam.data.lens = 42
    cam.data.clip_end = 5000
    scn.camera = cam
    track = add_empty("CamTarget", (fx, fy, target_z), size=1.0)
    con = cam.constraints.new("TRACK_TO")
    con.target = track
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"

    # мир
    scn.world = bpy.data.worlds.new("World") if not scn.world else scn.world
    scn.world.use_nodes = True
    bg = scn.world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.05, 0.06, 0.08, 1.0)
        bg.inputs["Strength"].default_value = 1.0

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out))
    log(f"сцена сохранена: {out} ({len(bpy.data.objects)} объектов, {len(frames)} кадров)")


def _hex_rgba(hexc, alpha=1.0):
    h = str(hexc).lstrip("#")
    if len(h) != 6:
        return (0.49, 0.91, 0.53, alpha)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (r, g, b, alpha)


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    main(argv)
