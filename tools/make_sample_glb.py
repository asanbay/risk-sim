"""
tools/make_sample_glb.py — синтетическая GLB-модель здания для sample-сценария.

Зачем: клиренс грузового пути (risk_sim.clearance) работает против меша
data/model.glb (импорт IFC). Чтобы проверять конвейер end-to-end без
настоящего IFC, строим упрощённую модель башни по зонам sample-сценария:
  * законченные этажи-плиты с фасадными стенами до яруса L61;
  * ядро CORE-L62 со стенами на верхнем ярусе;
  * фундаментная плита.
Экспорт: data/sample/model.glb + data/sample/model_import.json в формате
risk_sim.model.load_model_metadata (с sha256, как после import-ifc).

Запуск: python tools/make_sample_glb.py [--data data/sample]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import trimesh


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_box(scene: trimesh.Scene, name: str,
            x0: float, y0: float, z0: float,
            x1: float, y1: float, z1: float) -> None:
    box = trimesh.creation.box(extents=(abs(x1 - x0), abs(y1 - y0), abs(z1 - z0)))
    box.apply_translation(((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))
    scene.add_geometry(box, node_name=name, geom_name=name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/sample")
    ap.add_argument("--floor-step", type=float, default=4.0)
    a = ap.parse_args()

    data_dir = Path(a.data)
    zones = pd.read_csv(data_dir / "zones.csv")
    build = zones[zones["level"].isin(["L61", "L62"]) & (zones["type"] != "crane_hazard")]
    if build.empty:
        print("нет зон уровней L61/L62 — модель не построена")
        return 1

    x0 = float(build["x_min"].min()) - 1.5
    x1 = float(build["x_max"].max()) + 1.5
    y0 = float(build["y_min"].min()) - 1.5
    y1 = float(build["y_max"].max()) + 1.5
    lowest = float(build["z_floor"].min())
    core = build[build["zone_id"].str.contains("CORE", na=False)].iloc[0]

    scene = trimesh.Scene()
    add_box(scene, "Foundation", x0 - 2, y0 - 2, -0.6, x1 + 2, y1 + 2, 0.0)
    n_floors = 0
    fz = a.floor_step
    while fz < lowest - 1.0:
        add_box(scene, f"Slab_{n_floors:02d}", x0 + 0.3, y0 + 0.3, fz - 0.35, x1 - 0.3, y1 - 0.3, fz)
        add_box(scene, f"Facade_{n_floors:02d}", x0, y0, fz, x1, y1, fz + a.floor_step + 0.02)
        n_floors += 1
        fz += a.floor_step
    # ядро верхнего яруса со стенами
    t = 0.4
    cz_top = float(core["z_top"])
    cz_floor = float(core["z_floor"])
    add_box(scene, "Core_S", core["x_min"], core["y_min"], cz_floor, core["x_max"], core["y_min"] + t, cz_top)
    add_box(scene, "Core_N", core["x_min"], core["y_max"] - t, cz_floor, core["x_max"], core["y_max"], cz_top)
    add_box(scene, "Core_W", core["x_min"], core["y_min"], cz_floor, core["x_min"] + t, core["y_max"], cz_top)
    add_box(scene, "Core_E", core["x_max"] - t, core["y_min"], cz_floor, core["x_max"], core["y_max"], cz_top)

    glb_path = data_dir / "model.glb"
    scene.export(glb_path)
    triangles = int(sum(len(g.faces) for g in scene.geometry.values()))
    metadata = {
        "schema_version": "1.0",
        "source_file": "SYNTHETIC make_sample_glb.py",
        "source_sha256": _sha256(Path(__file__)),
        "ifc_schema": "synthetic",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "glb_file": glb_path.name,
        "glb_sha256": _sha256(glb_path),
        "coordinate_system": "right_handed_z_up_m",
        "glb_coordinates": "site_coordinates",
        "unit_scale_to_m": 1.0,
        "include_types": ["synthetic"],
        "element_count": len(scene.geometry),
        "triangle_count": triangles,
        "failed_element_count": 0,
        "failed_elements": [],
        "synthetic": True,
    }
    (data_dir / "model_import.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"модель: {glb_path} ({len(scene.geometry)} элементов, {triangles} треугольников, "
          f"этажей {n_floors}, footprint {x1-x0:.0f}×{y1-y0:.0f} м)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
