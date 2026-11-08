from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .io import InputError

DEFAULT_IFC_TYPES = (
    "IfcSlab",
    "IfcWall",
    "IfcColumn",
    "IfcBeam",
    "IfcMember",
    "IfcStair",
    "IfcRoof",
    "IfcCurtainWall",
    "IfcDoor",
    "IfcWindow",
    "IfcRailing",
    "IfcBuildingElementProxy",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_ifc_model(
    source_path: str | Path,
    data_dir: str | Path,
    include_types: tuple[str, ...] = DEFAULT_IFC_TYPES,
    max_triangles: int = 200_000,
) -> dict[str, Any]:
    try:
        import ifcopenshell
        import ifcopenshell.geom
        import ifcopenshell.util.unit
        import trimesh
    except ImportError as error:
        raise InputError("Для импорта IFC установите пакеты: pip install -e '.[bim]'") from error

    source = Path(source_path).resolve()
    if not source.is_file():
        raise InputError(f"IFC-файл не найден: {source}")
    if max_triangles < 1:
        raise InputError("max_triangles должен быть положительным")
    model = ifcopenshell.open(str(source))
    unit_scale = float(ifcopenshell.util.unit.calculate_unit_scale(model))
    elements = {}
    for ifc_type in include_types:
        for element in model.by_type(ifc_type):
            elements[element.id()] = element
    if not elements:
        raise InputError(f"В IFC не найдены поддерживаемые элементы: {', '.join(include_types)}")

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    scene = trimesh.Scene()
    triangle_count = 0
    failed = []
    for element in elements.values():
        try:
            shape = ifcopenshell.geom.create_shape(settings, element)
            faces = np.asarray(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
            vertices = np.asarray(shape.geometry.verts, dtype=np.float64).reshape(-1, 3) * unit_scale
            if not len(faces) or not len(vertices):
                continue
            triangle_count += len(faces)
            if triangle_count > max_triangles:
                raise InputError(
                    f"Лимит {max_triangles} треугольников превышен. "
                    "Сузьте --include-types или заранее упростите модель; неполный GLB не создан."
                )
            mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
            mesh.metadata.update({"global_id": element.GlobalId, "ifc_type": element.is_a()})
            scene.add_geometry(mesh, node_name=element.GlobalId, geom_name=f"{element.is_a()}_{element.id()}")
        except InputError:
            raise
        except Exception as error:
            failed.append({"global_id": element.GlobalId, "ifc_type": element.is_a(), "error": str(error)})
    if not scene.geometry:
        details = f"; ошибки: {failed[:3]}" if failed else ""
        raise InputError(f"IFC не дал пригодной геометрии{details}")

    destination = Path(data_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    glb_path = destination / "model.glb"
    scene.export(glb_path)
    metadata = {
        "schema_version": "1.0",
        "source_file": source.name,
        "source_sha256": _sha256(source),
        "ifc_schema": model.schema,
        "generated_at": datetime.now().astimezone().isoformat(),
        "glb_file": glb_path.name,
        "glb_sha256": _sha256(glb_path),
        "coordinate_system": "right_handed_z_up_m",
        "glb_coordinates": "ifc_coordinates_preserved",
        "unit_scale_to_m": unit_scale,
        "include_types": list(include_types),
        "element_count": len(scene.geometry),
        "triangle_count": int(sum(len(mesh.faces) for mesh in scene.geometry.values())),
        "failed_element_count": len(failed),
        "failed_elements": failed,
    }
    metadata_path = destination / "model_import.json"
    with metadata_path.open("w", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
    return metadata


def load_model_metadata(data_dir: str | Path) -> dict[str, Any] | None:
    directory = Path(data_dir).resolve()
    metadata_path = directory / "model_import.json"
    if not metadata_path.exists():
        return None
    with metadata_path.open(encoding="utf-8") as stream:
        metadata = json.load(stream)
    filename = metadata.get("glb_file")
    if not filename or Path(filename).name != filename:
        raise InputError("model_import.json: glb_file должен быть именем файла без путей")
    glb_path = directory / filename
    if not glb_path.is_file():
        raise InputError(f"model_import.json: не найден {glb_path}")
    if metadata.get("glb_sha256") != _sha256(glb_path):
        raise InputError("model.glb изменился после импорта; повторите import-ifc")
    return {**metadata, "url": "model.glb"}
