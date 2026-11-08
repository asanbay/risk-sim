from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector


def arguments() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--lift-id", required=True)
    parser.add_argument("--output", default="scenes/lift.blend")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--duration-s", type=float)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--render", action="store_true")
    return parser.parse_args(argv)


def material(name: str, color: tuple[float, float, float, float], alpha: float = 1.0) -> bpy.types.Material:
    result = bpy.data.materials.new(name)
    result.diffuse_color = (*color[:3], alpha)
    result.use_nodes = True
    bsdf = result.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*color[:3], alpha)
    bsdf.inputs["Alpha"].default_value = alpha
    if alpha < 1:
        result.surface_render_method = "DITHERED"
    return result


def box(name: str, center: tuple[float, float, float], dimensions: tuple[float, float, float], mat: bpy.types.Material) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(size=1, location=center)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(mat)
    return obj


def cylinder_between(name: str, start: Vector, end: Vector, radius: float, mat: bpy.types.Material) -> bpy.types.Object:
    direction = end - start
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=radius, depth=direction.length, location=(start + end) / 2)
    obj = bpy.context.object
    obj.name = name
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = direction.to_track_quat("Z", "Y")
    obj.data.materials.append(mat)
    return obj


def main() -> None:
    args = arguments()
    scenario_path = Path(args.scenario).resolve()
    with scenario_path.open(encoding="utf-8") as stream:
        scenario = json.load(stream)
    lift = next(item for item in scenario["lifts"] if item["summary"]["lift_id"] == args.lift_id)
    source, result = lift["input"], lift["summary"]
    duration = min(args.duration_s if args.duration_s is not None else result["duration_s"], result["duration_s"])
    if duration <= 0 or args.fps < 1 or args.width < 16 or args.height < 16:
        raise ValueError("Длительность, fps и разрешение должны быть положительными")
    track = [point for point in lift["track"] if point["t"] <= duration]
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.fps = args.fps
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene["scenario_id"] = scenario["metadata"]["scenario_id"]
    scene["source_sha256"] = json.dumps(scenario["metadata"]["source_sha256"], ensure_ascii=False)
    scene["disclaimer"] = scenario["metadata"]["disclaimer"]

    concrete = material("Concrete", (0.32, 0.39, 0.46, 1))
    active_concrete = material("Active concrete", (0.48, 0.58, 0.65, 1))
    steel = material("Crane yellow", (0.95, 0.55, 0.06, 1))
    load_mat = material("Load orange", (0.9, 0.24, 0.05, 1))
    zone_mats = {
        "work": material("Work zone", (0.02, 0.48, 0.8, 1), 0.12),
        "crane_hazard": material("Crane hazard", (0.95, 0.03, 0.06, 1), 0.16),
        "edge": material("Edge", (1.0, 0.5, 0.02, 1), 0.18),
        "passage": material("Passage", (0.02, 0.7, 0.4, 1), 0.1),
        "storage": material("Storage", (0.1, 0.65, 0.35, 1), 0.1),
    }
    building = scenario["building"]
    footprint = building["footprint"]
    width = footprint["x_max"] - footprint["x_min"]
    depth = footprint["y_max"] - footprint["y_min"]
    center_x = (footprint["x_max"] + footprint["x_min"]) / 2
    center_y = (footprint["y_max"] + footprint["y_min"]) / 2
    for floor in range(int(building["active_floor"]) + 1):
        z = floor * building["floor_height_m"]
        box(f"Slab_{floor + 1:03d}", (center_x, center_y, z), (width, depth, 0.16), active_concrete if floor == building["active_floor"] else concrete)
    core = building["core"]
    box("Core", ((core["x_min"] + core["x_max"]) / 2, (core["y_min"] + core["y_max"]) / 2, building["height_m"] / 2), (core["x_max"] - core["x_min"], core["y_max"] - core["y_min"], building["height_m"]), concrete)
    for zone in scenario["zones"]:
        obj = box(zone["zone_id"], ((zone["x_min"] + zone["x_max"]) / 2, (zone["y_min"] + zone["y_max"]) / 2, (zone["z_floor"] + zone["z_top"]) / 2), (zone["x_max"] - zone["x_min"], zone["y_max"] - zone["y_min"], zone["z_top"] - zone["z_floor"]), zone_mats.get(zone["type"], zone_mats["work"]))
        obj.display_type = "WIRE"

    pivot = Vector(result["pivot_m"])
    crane_x, crane_y = source.get("crane_x_m", 0), source.get("crane_y_m", 0)
    box("CraneTower", (crane_x, crane_y, pivot.z / 2), (1.2, 1.2, pivot.z), steel)
    cylinder_between("CraneJib", Vector((crane_x, crane_y, pivot.z)), pivot, 0.45, steel)
    dimensions = result["load_dimensions_m"]
    load = box("Load", tuple(lift["track"][0]["pos"]), tuple(dimensions), load_mat)
    load.rotation_mode = "QUATERNION"
    cable = cylinder_between("CraneCable", pivot, Vector(lift["track"][0]["pos"]), 0.025, steel)
    cable.rotation_mode = "QUATERNION"

    timeline_start = scenario["timeline"]["start"]
    crew_colors = {}
    for agent in scenario["timeline"]["agents"]:
        color = agent["color"].lstrip("#")
        rgb = tuple(int(color[index:index + 2], 16) / 255 for index in (0, 2, 4))
        crew_colors[agent["id"]] = material(f"Crew {agent['crew_id']}", (*rgb, 1))
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=0.32)
        obj = bpy.context.object
        obj.name = agent["id"]
        obj.scale = (1, 1, 2.4)
        obj.data.materials.append(crew_colors[agent["id"]])
        first = next(values for frame in scenario["timeline"]["frames"] for values in frame["agents"] if values[0] == agent["id"])
        obj.location = first[1:4]
        obj.location.z += 0.75
        obj["crew_id"] = agent["crew_id"]
        obj["task_id"] = agent["task_id"]

    from datetime import datetime
    start = datetime.fromisoformat(timeline_start)
    frames = scenario["timeline"]["frames"]
    rest_cable_length = (Vector(lift["track"][0]["pos"]) - pivot).length
    for point in track:
        frame_number = 1 + round(point["t"] * args.fps)
        position = Vector(point["pos"])
        load.location = position
        load.rotation_quaternion = Quaternion(point["quat"])
        load.keyframe_insert(data_path="location", frame=frame_number)
        load.keyframe_insert(data_path="rotation_quaternion", frame=frame_number)
        end = position + Vector((0, 0, dimensions[2] / 2))
        direction = end - pivot
        cable.location = (pivot + end) / 2
        cable.rotation_quaternion = direction.to_track_quat("Z", "Y")
        cable.scale = (1, 1, direction.length / rest_cable_length)
        cable.keyframe_insert(data_path="location", frame=frame_number)
        cable.keyframe_insert(data_path="rotation_quaternion", frame=frame_number)
        cable.keyframe_insert(data_path="scale", frame=frame_number)
        timestamp = datetime.fromisoformat(point["timestamp"])
        current = (timestamp - start).total_seconds()
        nearest = min(frames, key=lambda item: abs((datetime.fromisoformat(item["timestamp"]) - start).total_seconds() - current))
        for values in nearest["agents"]:
            obj = bpy.data.objects[values[0]]
            obj.location = values[1:4]
            obj.location.z += 0.75
            obj.keyframe_insert(data_path="location", frame=frame_number)

    scene.frame_start = 1
    scene.frame_end = 1 + round(duration * args.fps)
    scene.render.resolution_x = args.width
    scene.render.resolution_y = args.height
    scene.render.resolution_percentage = 100
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    if hasattr(scene.render.image_settings, "media_type"):
        scene.render.image_settings.media_type = "VIDEO"
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scene.render.filepath = str(Path(args.output).with_suffix(".mp4").resolve())
    distance = max(30.0, max(width, depth) * 1.1)
    target = Vector((center_x, center_y, building["active_z_m"] + 1.5))
    bpy.ops.object.camera_add(location=(center_x + distance, center_y - distance, pivot.z + distance * 0.45))
    camera = bpy.context.object
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = camera
    bpy.ops.object.light_add(type="SUN", location=(30, -30, pivot.z + 50))
    bpy.context.object.rotation_euler = (math.radians(25), math.radians(-20), math.radians(-30))
    bpy.context.object.data.energy = 3
    scene.world.color = (0.15, 0.15, 0.15)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output))
    if args.render:
        bpy.ops.render.render(animation=True)


if __name__ == "__main__":
    main()
