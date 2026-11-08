from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import DISCLAIMER, __version__

LAYERS = ("animation", "risks", "crews", "crane", "geometry")


def _name(value: Any) -> str:
    result = re.sub(r"[^A-Za-z0-9_]", "_", str(value))
    return f"_{result}" if result[:1].isdigit() else result


def _number(value: Any) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Не удалось записать нечисловое значение в USDA: {value!r}")
    return format(number, ".9g")


def _vector(values: Iterable[Any]) -> str:
    return "(" + ", ".join(_number(value) for value in values) + ")"


def _string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _color(value: str) -> tuple[float, float, float]:
    normalized = value.lstrip("#")
    if len(normalized) != 6:
        return 0.2, 0.55, 0.9
    return tuple(int(normalized[index:index + 2], 16) / 255 for index in (0, 2, 4))


def _header(fps: float, end_time: float, sublayers: bool = False, layer_names: Iterable[str] = LAYERS) -> list[str]:
    lines = [
        "#usda 1.0",
        "(",
        '    defaultPrim = "Site"',
        "    metersPerUnit = 1",
        '    upAxis = "Z"',
        f"    timeCodesPerSecond = {_number(fps)}",
        "    startTimeCode = 0",
        f"    endTimeCode = {_number(end_time)}",
    ]
    if sublayers:
        lines.append("    subLayers = [")
        lines.extend(f"        @{layer}.usda@," for layer in layer_names)
        lines.append("    ]")
    lines.append(")")
    return lines


def _cube(
    name: str,
    center: Iterable[float],
    dimensions: Iterable[float],
    color: tuple[float, float, float] = (0.5, 0.5, 0.5),
    opacity: float = 1.0,
    indent: int = 2,
    visibility: str | None = None,
) -> list[str]:
    prefix = " " * indent
    lines = [
        f'{prefix}def Cube "{_name(name)}" {{',
        f"{prefix}    double size = 1",
        f"{prefix}    double3 xformOp:translate = {_vector(center)}",
        f"{prefix}    double3 xformOp:scale = {_vector(dimensions)}",
        f'{prefix}    token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]',
        f"{prefix}    color3f[] primvars:displayColor = [{_vector(color)}]",
        f"{prefix}    float[] primvars:displayOpacity = [{_number(opacity)}]",
    ]
    if visibility:
        lines.append(f'{prefix}    token visibility = "{visibility}"')
    lines.append(f"{prefix}}}")
    return lines


def _capsule(name: str, center: Iterable[float], color: tuple[float, float, float], indent: int) -> list[str]:
    prefix = " " * indent
    return [
        f'{prefix}def Capsule "{_name(name)}" {{',
        f"{prefix}    double radius = 0.32",
        f"{prefix}    double height = 0.8",
        f'{prefix}    token axis = "Z"',
        f"{prefix}    double3 xformOp:translate = {_vector(center)}",
        f'{prefix}    token[] xformOpOrder = ["xformOp:translate"]',
        f"{prefix}    color3f[] primvars:displayColor = [{_vector(color)}]",
        f"{prefix}}}",
    ]


def _samples(type_name: str, attribute: str, values: list[tuple[float, Any]], indent: int) -> list[str]:
    prefix = " " * indent
    lines = [f"{prefix}{type_name} {attribute}.timeSamples = {{"]
    unique = {timecode: value for timecode, value in values}
    for timecode in sorted(unique):
        value = unique[timecode]
        if isinstance(value, str):
            rendered = _string(value)
        elif isinstance(value, (list, tuple)):
            rendered = _vector(value)
        else:
            rendered = _number(value)
        lines.append(f"{prefix}    {_number(timecode)}: {rendered},")
    lines.append(f"{prefix}}}")
    return lines


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _timecode(value: str, origin: datetime, fps: float) -> float:
    return round((_timestamp(value) - origin).total_seconds() * fps, 3)


def _sampled(items: list[Any], every: int) -> list[Any]:
    if not items:
        return []
    result = items[::every]
    if result[-1] is not items[-1]:
        result.append(items[-1])
    return result


def _geometry_layer(bundle: dict[str, Any], fps: float, end_time: float) -> list[str]:
    zones = bundle["timeline"]["zones"]
    lines = _header(fps, end_time)
    lines += ['def Xform "Site" {']
    if not bundle.get("model"):
        lines += ['    def Xform "Building" {']
        levels: dict[str, list[dict[str, Any]]] = {}
        for zone in zones:
            levels.setdefault(zone.get("level", "?"), []).append(zone)
        for level, items in sorted(levels.items()):
            if level == "G":
                continue
            z0 = min(zone["z_floor"] for zone in items)
            x0 = min(zone["x_min"] for zone in items) - 2
            x1 = max(zone["x_max"] for zone in items) + 2
            y0 = min(zone["y_min"] for zone in items) - 2
            y1 = max(zone["y_max"] for zone in items) + 2
            lines += _cube(f"Slab_{level}", ((x0 + x1) / 2, (y0 + y1) / 2, z0 - 0.225), (x1 - x0, y1 - y0, 0.45), (0.25, 0.29, 0.35), indent=8)
        core = [zone for zone in zones if zone.get("type") == "work" and "CORE" in zone["id"]]
        if core:
            zone = max(core, key=lambda item: item["z_floor"])
            ztop = max(item["z_top"] for item in zones)
            lines += _cube(
                "Core",
                ((zone["x_min"] + zone["x_max"]) / 2, (zone["y_min"] + zone["y_max"]) / 2, (zone["z_floor"] - 4 + ztop + 6) / 2),
                (zone["x_max"] - zone["x_min"], zone["y_max"] - zone["y_min"], ztop + 10 - zone["z_floor"]),
                (0.2, 0.24, 0.3), indent=8,
            )
        lines += ["    }"]
    lines += ['    def Xform "Zones" {']
    colors = {"work": (0.3, 0.64, 1.0), "edge": (1.0, 0.55, 0.1), "passage": (0.1, 0.75, 0.45), "storage": (0.65, 0.35, 0.9), "crane_hazard": (1.0, 0.35, 0.2)}
    for zone in zones:
        height = 1.5 if zone["type"] == "edge" else min(max(0.6, zone["z_top"] - zone["z_floor"]), 3.0)
        lines += [f'        def Xform "{_name(zone["id"])}" {{', f'            custom string zoneType = {_string(zone["type"])}', f'            custom string level = {_string(zone.get("level", ""))}']
        lines += _cube(
            "Footprint",
            ((zone["x_min"] + zone["x_max"]) / 2, (zone["y_min"] + zone["y_max"]) / 2, zone["z_floor"] + height / 2),
            (zone["x_max"] - zone["x_min"], zone["y_max"] - zone["y_min"], height),
            colors.get(zone["type"], colors["work"]), 0.2, indent=12,
        )
        lines.append("        }")
    lines += ["    }", "}"]
    return lines


def _array(type_name: str, attribute: str, values: Any, formatter: Any, indent: int, per_line: int = 12) -> list[str]:
    prefix = " " * indent
    lines = [f"{prefix}{type_name} {attribute} = ["]
    for start in range(0, len(values), per_line):
        chunk = values[start:start + per_line]
        lines.append(f"{prefix}    {', '.join(formatter(value) for value in chunk)},")
    lines.append(f"{prefix}]")
    return lines


def _ifc_mesh_layer(bundle: dict[str, Any], bundle_dir: Path, fps: float, end_time: float, metrics: dict[str, int]) -> list[str]:
    model = bundle["model"]
    if model.get("glb_coordinates") != "ifc_coordinates_preserved":
        raise ValueError("Для USD требуется подтверждённая система glb_coordinates=ifc_coordinates_preserved")
    filename = model["glb_file"]
    if Path(filename).name != filename:
        raise ValueError("glb_file должен быть именем файла без путей")
    glb_path = bundle_dir / filename
    if not glb_path.is_file():
        raise FileNotFoundError(f"Не найден GLB для USD: {glb_path}")
    if model.get("glb_sha256") and model["glb_sha256"] != _sha256(glb_path):
        raise ValueError("GLB изменился после импорта IFC; повторите import-ifc и расчёт")
    try:
        import numpy as np
        import trimesh
    except ImportError as error:
        raise RuntimeError("Для преобразования IFC/GLB в USD установите пакеты: pip install -e '.[bim]'") from error

    scene = trimesh.load(glb_path, force="scene")
    lines = _header(fps, end_time)
    lines += [
        'def Xform "Site" {',
        '    def Xform "IFC_Model" {',
        f'        custom string sourceFile = {_string(model["source_file"])}',
        f'        custom string sourceSha256 = {_string(model["source_sha256"])}',
        f'        custom string glbSha256 = {_string(model["glb_sha256"])}',
        '        custom string coordinateSystem = "right_handed_z_up_m"',
    ]
    for node_name in scene.graph.nodes_geometry:
        transform, geometry_name = scene.graph[node_name]
        mesh = scene.geometry[geometry_name].copy()
        mesh.apply_transform(transform)
        vertices = np.asarray(mesh.vertices, dtype=float)
        faces = np.asarray(mesh.faces, dtype=np.int64)
        if not len(vertices) or not len(faces):
            continue
        metrics["triangle_count"] = metrics.get("triangle_count", 0) + len(faces)
        if metrics["triangle_count"] > 200_000:
            raise ValueError("Для USD превышен лимит 200000 треугольников; упростите IFC перед экспортом")
        metrics["mesh_count"] = metrics.get("mesh_count", 0) + 1
        metadata = mesh.metadata
        lines += [
            f'        def Xform "{_name(node_name)}" {{',
            f'            custom string globalId = {_string(metadata.get("global_id", node_name))}',
            f'            custom string ifcType = {_string(metadata.get("ifc_type", "Unknown"))}',
            '            def Mesh "Geometry" {',
            '                token orientation = "rightHanded"',
            '                color3f[] primvars:displayColor = [(0.55, 0.62, 0.68)]',
        ]
        lines += _array("point3f[]", "points", vertices, _vector, 16)
        lines += _array("int[]", "faceVertexCounts", np.full(len(faces), 3), lambda value: str(int(value)), 16)
        lines += _array("int[]", "faceVertexIndices", faces.reshape(-1), lambda value: str(int(value)), 16)
        lines += ["            }", "        }"]
    if not metrics.get("mesh_count"):
        raise ValueError("GLB не содержит пригодных для USD треугольников")
    lines += ["    }", "}"]
    return lines


def _crane_layer(bundle: dict[str, Any], frames: list[dict[str, Any]], origin: datetime, fps: float, end_time: float) -> list[str]:
    crane = bundle["timeline"]["crane"]
    mast, jib = crane["mast"], crane["jib"]
    mx, my, top = mast["x"], mast["y"], mast["top_z"]
    section = mast["section_m"]
    first = next((frame["crane"] for frame in frames if frame.get("crane")), {})
    radius = first.get("radius_m", jib["length_m"] / 2)
    hook_z = first.get("hook_z_m", top - 20)
    hook_offset = hook_z - (top + 2.4)
    lines = _header(fps, end_time)
    lines += ['def Xform "Site" {', '    def Xform "Crane" {', f'        custom string craneId = {_string(crane["id"])}', f'        custom string craneModel = {_string(crane["model"])}']
    lines += _cube("Mast", (mx, my, (mast["base_z"] + top) / 2), (section, section, top - mast["base_z"]), (1.0, 0.62, 0.05), indent=8)
    lines += _cube("Head", (mx, my, top + 4.5), (5, 5, 9), (1.0, 0.62, 0.05), indent=8)
    lines += [
        '        def Xform "Slew" {',
        f"            double3 xformOp:translate = {_vector((mx, my, top + 4))}",
        "            double xformOp:rotateZ = 0",
        '            token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateZ"]',
    ]
    lines += _cube("Jib", (jib["length_m"] / 2, 0, 0), (jib["length_m"], 1.6, 1.2), (1.0, 0.62, 0.05), indent=12)
    counter = jib.get("counter_jib_m", jib["length_m"] / 3)
    lines += _cube("CounterJib", (-counter / 2, 0, 0), (counter, 1.8, 1.2), (1.0, 0.62, 0.05), indent=12)
    lines += [
        '            def Xform "Trolley" {',
        f"                double3 xformOp:translate = {_vector((radius, 0, -1.6))}",
        '                token[] xformOpOrder = ["xformOp:translate"]',
    ]
    lines += _cube("TrolleyBody", (0, 0, 0), (2.8, 2.2, 1.6), (1.0, 0.62, 0.05), indent=16)
    lines += [
        '                def Xform "Hook" {',
        f"                    double3 xformOp:translate = {_vector((0, 0, hook_offset))}",
        '                    token[] xformOpOrder = ["xformOp:translate"]',
    ]
    lines += _cube("HookMarker", (0, 0, 0), (0.6, 0.6, 0.6), (0.9, 0.8, 0.2), indent=20)
    lines += ["                }"]
    lines += [
        '                def Cylinder "Rope" {',
        "                    double radius = 0.07",
        "                    double height = 1",
        '                    token axis = "Z"',
        f"                    double3 xformOp:translate = {_vector((0, 0, hook_offset / 2))}",
        f"                    double3 xformOp:scale = {_vector((1, 1, max(abs(hook_offset), 0.001)))}",
        '                    token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]',
        "                    color3f[] primvars:displayColor = [(0.8, 0.75, 0.5)]",
        "                }",
        "            }",
        "        }",
        "    }",
        '    def Xform "Loads" {',
    ]
    for lift in bundle.get("lifts", []):
        source = lift["input"]
        pickup = source.get("pickup", [0.0, 0.0, 0.0])
        lines += [
            f'        def Xform "{_name(lift["lift_id"])}" {{',
            '            token visibility = "invisible"',
            f'            custom string liftId = {_string(lift["lift_id"])}',
            f'            custom string taskId = {_string(lift["task_id"])}',
            f'            custom double massKg = {_number(source["mass_kg"])}',
            f'            custom double areaM2 = {_number(source["area_m2"])}',
            f'            custom double windLimitMs = {_number(source["wind_limit_ms"])}',
            f"            double3 xformOp:translate = {_vector(pickup)}",
            '            token[] xformOpOrder = ["xformOp:translate"]',
        ]
        lines += _cube("LoadProxy", (0, 0, 0), (2.0, 1.0, 1.0), (0.95, 0.35, 0.08), indent=12)
        lines.append("        }")
    lines += ["    }", "}"]
    return lines


def _crews_layer(bundle: dict[str, Any], origin: datetime, fps: float, end_time: float) -> list[str]:
    tracks = {track["crew_id"]: track for track in bundle["timeline"].get("crew_tracks", [])}
    lines = _header(fps, end_time)
    lines += ['def Xform "Site" {', '    def Xform "Crews" {']
    for crew in bundle["timeline"].get("crews", []):
        track = tracks.get(crew["crew_id"], {})
        points = track.get("points", [])
        first = points[0] if points else {"x": 0, "y": 0, "z": 0}
        lines += [
            f'        def Xform "{_name(crew["crew_id"])}" {{',
            f'            custom string crewId = {_string(crew["crew_id"])}',
            f'            custom string crewName = {_string(crew.get("name", ""))}',
            f'            custom int size = {int(crew["size"])}',
            f"            double3 xformOp:translate = {_vector((first['x'], first['y'], first['z']))}",
            '            token[] xformOpOrder = ["xformOp:translate"]',
        ]
        for index in range(min(int(crew["size"]), 10)):
            offset = ((index % 3 - 1) * 1.15, ((index // 3) % 3 - 1) * 1.15, 0.9)
            lines += _capsule(f"Worker_{index + 1:02d}", offset, _color(crew.get("color", "#3b82f6")), 12)
        lines.append("        }")
    lines += ["    }", "}"]
    return lines


def _risks_layer(bundle: dict[str, Any], fps: float, end_time: float) -> list[str]:
    lines = _header(fps, end_time)
    lines += ['def Xform "Site" {', '    def Xform "Risks" {']
    for zone in bundle["timeline"]["zones"]:
        related = [finding for finding in bundle.get("findings", []) if zone["id"] in finding.get("zone_ids", [])]
        rules = sorted({finding["rule_id"] for finding in related})
        sources = sorted({finding["source"] for finding in related})
        height = 1.5 if zone["type"] == "edge" else min(max(0.6, zone["z_top"] - zone["z_floor"]), 3.0)
        lines += [
            f'        def Xform "{_name(zone["id"])}" {{',
            f'            custom string[] ruleIds = [{", ".join(_string(rule) for rule in rules)}]',
            f'            custom string[] sources = [{", ".join(_string(source) for source in sources)}]',
        ]
        lines += _cube(
            "Hazard",
            ((zone["x_min"] + zone["x_max"]) / 2, (zone["y_min"] + zone["y_max"]) / 2, zone["z_floor"] + height / 2),
            (zone["x_max"] - zone["x_min"], zone["y_max"] - zone["y_min"], height),
            (1.0, 0.05, 0.05), 0.45, indent=12, visibility="invisible",
        )
        lines.append("        }")
    lines += ["    }", "}"]
    return lines


def _risk_samples(bundle: dict[str, Any], origin: datetime, fps: float) -> dict[str, list[tuple[float, str]]]:
    intervals: dict[str, list[tuple[float, float]]] = {}
    for finding in bundle.get("findings", []):
        start = _timecode(finding["start"], origin, fps)
        end = _timecode(finding["end"], origin, fps)
        for zone_id in finding.get("zone_ids", []):
            intervals.setdefault(zone_id, []).append((start, end))
    result = {}
    for zone_id, items in intervals.items():
        merged = []
        for start, end in sorted(items):
            if end <= 0:
                continue
            start = max(0.0, start)
            if end <= start:
                end = start + 1
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        samples = [(0.0, "invisible")]
        for start, end in merged:
            samples.extend(((start, "inherited"), (end, "invisible")))
        result[zone_id] = samples
    return result


def _animation_layer(bundle: dict[str, Any], frames: list[dict[str, Any]], origin: datetime, fps: float, end_time: float) -> list[str]:
    crane = bundle["timeline"]["crane"]
    top = crane["mast"]["top_z"]
    lines = _header(fps, end_time)
    lines += ['def Xform "Site" {', '    def Xform "Environment" {']
    for attribute, key in (("gustAtRefMs", "gust_at_ref_ms"), ("gustAtTopMs", "gust_at_top_ms"), ("meanAtRefMs", "mean_at_ref_ms")):
        values = [(_timecode(frame["t"], origin, fps), frame["wind"][key]) for frame in frames if frame.get("wind")]
        lines += _samples("custom double", attribute, values, 8)
    lines += ["    }"]
    crane_frames = [frame for frame in frames if frame.get("crane")]
    if crane_frames:
        lines += ['    over "Crane" {', '        over "Slew" {']
        lines += _samples("double", "xformOp:rotateZ", [(_timecode(frame["t"], origin, fps), frame["crane"]["slew_deg"]) for frame in crane_frames], 12)
        lines += ['            over "Trolley" {']
        lines += _samples("double3", "xformOp:translate", [(_timecode(frame["t"], origin, fps), (frame["crane"]["radius_m"], 0, -1.6)) for frame in crane_frames], 16)
        lines += ['                over "Hook" {']
        lines += _samples("double3", "xformOp:translate", [(_timecode(frame["t"], origin, fps), (0, 0, frame["crane"]["hook_z_m"] - (top + 2.4))) for frame in crane_frames], 20)
        lines += ["                }", '                over "Rope" {']
        rope_values = []
        rope_scale = []
        for frame in crane_frames:
            offset = frame["crane"]["hook_z_m"] - (top + 2.4)
            rope_values.append((_timecode(frame["t"], origin, fps), (0, 0, offset / 2)))
            rope_scale.append((_timecode(frame["t"], origin, fps), (1, 1, max(abs(offset), 0.001))))
        lines += _samples("double3", "xformOp:translate", rope_values, 20)
        lines += _samples("double3", "xformOp:scale", rope_scale, 20)
        lines += ["                }", "            }", "        }", "    }"]
    lines += ['    over "Loads" {']
    for lift in bundle.get("lifts", []):
        lift_id = lift["lift_id"]
        positions = []
        phases = []
        angles = []
        visibility = [(0.0, "invisible")]
        last_position = lift["input"].get("pickup", [0.0, 0.0, 0.0])
        for frame in frames:
            active = next((item for item in frame.get("loads", []) if item["lift_id"] == lift_id), None)
            timecode = _timecode(frame["t"], origin, fps)
            if active:
                last_position = (active["x"], active["y"], active["z"])
                phases.append((timecode, active["phase"]))
                angles.append((timecode, active.get("swing_theta_deg", 0.0)))
                visibility.append((timecode, "inherited"))
            else:
                visibility.append((timecode, "invisible"))
            positions.append((timecode, last_position))
        lines += [f'        over "{_name(lift_id)}" {{']
        lines += _samples("double3", "xformOp:translate", positions, 12)
        lines += _samples("token", "visibility", visibility, 12)
        if phases:
            lines += _samples("custom token", "phase", phases, 12)
        if angles:
            lines += _samples("custom double", "swingThetaDeg", angles, 12)
        lines += ["        }"]
    lines += ["    }"]
    lines += ['    over "Crews" {']
    for track in bundle["timeline"].get("crew_tracks", []):
        points = track.get("points", [])
        if not points:
            continue
        values = [(_timecode(point["t"], origin, fps), (point["x"], point["y"], point["z"])) for point in points]
        lines += [f'        over "{_name(track["crew_id"])}" {{']
        lines += _samples("double3", "xformOp:translate", values, 12)
        lines += ["        }"]
    lines += ["    }"]
    risk_samples = _risk_samples(bundle, origin, fps)
    lines += ['    over "Risks" {']
    for zone in bundle["timeline"]["zones"]:
        if zone["id"] not in risk_samples:
            continue
        lines += [f'        over "{_name(zone["id"])}" {{', '            over "Hazard" {']
        lines += _samples("token", "visibility", risk_samples[zone["id"]], 16)
        lines += ["            }", "        }"]
    lines += ["    }", "}"]
    return lines


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def flatten_openusd(stage_path: str | Path, blender_path: str | None = None) -> dict[str, Path]:
    source = Path(stage_path).resolve()
    executable = blender_path or os.environ.get("BLENDER")
    if not executable:
        candidates = [
            Path.home() / "Library/Application Support/Steam/steamapps/common/Blender/Blender.app/Contents/MacOS/Blender",
            Path("/Applications/Blender.app/Contents/MacOS/Blender"),
        ]
        executable = next((str(path) for path in candidates if path.is_file()), None) or shutil.which("blender")
    if not executable:
        raise RuntimeError("Blender с pxr не найден; укажите --blender /путь/к/Blender")
    script = Path(__file__).resolve().parents[1] / "blender" / "flatten_usd.py"
    output = source.with_name(f"{source.stem}_flattened.usda")
    validation = source.with_name("flatten_validation.json")
    command = [
        str(executable), "--factory-startup", "-b", "--python-exit-code", "1",
        "-P", str(script), "--", "--input", str(source), "--output", str(output),
        "--validation", str(validation),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Usd.Stage.Flatten завершился с ошибкой:\n{result.stdout}\n{result.stderr}")
    if not output.is_file() or not validation.is_file():
        raise RuntimeError("Flatten не создал flat-USD или flatten_validation.json")
    return {"stage": output, "validation": validation}


def export_openusd(
    bundle_path: str | Path,
    output_dir: str | Path,
    fps: float = 24.0,
    every: int = 1,
    flatten: bool = False,
    blender_path: str | None = None,
) -> dict[str, Any]:
    if fps <= 0 or every < 1:
        raise ValueError("fps должен быть положительным, every — целым >= 1")
    source = Path(bundle_path).resolve()
    bundle = json.loads(source.read_text(encoding="utf-8"))
    timeline = bundle["timeline"]
    origin = _timestamp(timeline["meta"]["t_start"])
    frames = _sampled(timeline["frames"], every)
    crew_tracks = []
    for track in timeline.get("crew_tracks", []):
        crew_tracks.append({**track, "points": _sampled(track.get("points", []), every)})
    bundle = {**bundle, "timeline": {**timeline, "frames": frames, "crew_tracks": crew_tracks}}
    all_times = [_timecode(frame["t"], origin, fps) for frame in frames]
    all_times += [_timecode(point["t"], origin, fps) for track in crew_tracks for point in track["points"]]
    all_times += [_timecode(finding[key], origin, fps) for finding in bundle.get("findings", []) for key in ("start", "end")]
    end_time = max(all_times, default=0.0)

    ifc_metrics: dict[str, int] = {}
    layer_names = ["animation", "risks", "crews", "crane"]
    if bundle.get("model"):
        layer_names.append("ifc_mesh")
    layer_names.append("geometry")
    writers = {
        "geometry": lambda: _geometry_layer(bundle, fps, end_time),
        "crane": lambda: _crane_layer(bundle, frames, origin, fps, end_time),
        "crews": lambda: _crews_layer(bundle, origin, fps, end_time),
        "risks": lambda: _risks_layer(bundle, fps, end_time),
        "animation": lambda: _animation_layer(bundle, frames, origin, fps, end_time),
    }
    if bundle.get("model"):
        writers["ifc_mesh"] = lambda: _ifc_mesh_layer(bundle, source.parent, fps, end_time, ifc_metrics)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = {}
    for layer, writer in writers.items():
        path = output / f"{layer}.usda"
        path.write_text("\n".join(writer()) + "\n", encoding="utf-8")
        paths[layer] = path
    stage_path = output / "stage.usda"
    stage_path.write_text("\n".join(_header(fps, end_time, sublayers=True, layer_names=layer_names)) + '\n\ndef Xform "Site" {\n}\n', encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "software_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_file": source.name,
        "source_sha256": _sha256(source),
        "stage_file": stage_path.name,
        "coordinate_system": "right_handed_z_up_m",
        "up_axis": "Z",
        "meters_per_unit": 1.0,
        "time_codes_per_second": fps,
        "end_time_code": end_time,
        "sampling_every": every,
        "layers": [{"file": f"{layer}.usda", "role": layer} for layer in layer_names],
        "composition_note": "subLayers записаны от сильного к слабому. stage_flattened.usda создаётся через Usd.Stage.Flatten для совместимости с Blender.",
        "counts": {
            "timeline_frames": len(frames),
            "zones": len(timeline["zones"]),
            "crews": len(timeline.get("crews", [])),
            "lifts": len(bundle.get("lifts", [])),
            "findings": len(bundle.get("findings", [])),
        },
        "ifc_model": bundle.get("model"),
        "geometry_note": (
            "IFC-треугольники преобразованы в ifc_mesh.usda с исходными GlobalId и ifc_type; кран и грузы остаются визуальными прокси."
            if bundle.get("model") else
            "Строительные элементы и грузы — упрощённые визуальные прокси. Исходная IFC-модель не задана."
        ),
        "disclaimer": DISCLAIMER,
    }
    manifest["counts"].update(ifc_metrics)
    manifest_path = output / "manifest.json"
    result = {"stage": stage_path, "manifest": manifest_path, "layers": paths, "counts": manifest["counts"]}
    if flatten:
        flattened = flatten_openusd(stage_path, blender_path)
        manifest["flattened_stage"] = flattened["stage"].name
        manifest["flatten_validation"] = flattened["validation"].name
        result["flattened_stage"] = flattened["stage"]
        result["flatten_validation"] = flattened["validation"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
