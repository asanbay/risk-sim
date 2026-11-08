from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from risk_sim.usd import export_openusd


def _bundle() -> dict:
    return {
        "timeline": {
            "meta": {"t_start": "2026-11-16 07:00", "dt_min": 10},
            "crane": {
                "id": "CR-TEST", "model": "Test crane",
                "mast": {"x": 0, "y": 0, "base_z": 0, "top_z": 100, "section_m": 2},
                "jib": {"length_m": 50, "counter_jib_m": 15},
                "limits": {},
            },
            "zones": [
                {"id": "Z-WORK", "level": "L1", "type": "work", "x_min": 0, "y_min": 0, "x_max": 10, "y_max": 10, "z_floor": 80, "z_top": 84},
                {"id": "Z-EDGE", "level": "L1", "type": "edge", "x_min": 10, "y_min": 0, "x_max": 12, "y_max": 10, "z_floor": 80, "z_top": 84},
            ],
            "crews": [{"crew_id": "C1", "name": "Test crew", "size": 2, "color": "#3b82f6"}],
            "crew_tracks": [{
                "crew_id": "C1", "name": "Test crew", "size": 2, "color": "#3b82f6",
                "points": [
                    {"t": "2026-11-16 07:00", "x": 1, "y": 1, "z": 80, "state": "working"},
                    {"t": "2026-11-16 07:10", "x": 2, "y": 2, "z": 80, "state": "working"},
                ],
            }],
            "frames": [
                {
                    "t": "2026-11-16 07:00",
                    "wind": {"gust_at_ref_ms": 5, "gust_at_top_ms": 8, "mean_at_ref_ms": 4},
                    "crane": {"slew_deg": 0, "radius_m": 20, "hook_z_m": 82},
                    "loads": [{"lift_id": "L-01", "x": 1, "y": 2, "z": 82, "phase": "lift", "swing_theta_deg": 1}],
                },
                {
                    "t": "2026-11-16 07:10",
                    "wind": {"gust_at_ref_ms": 6, "gust_at_top_ms": 9, "mean_at_ref_ms": 4.5},
                    "crane": {"slew_deg": 45, "radius_m": 30, "hook_z_m": 75},
                    "loads": [],
                },
            ],
        },
        "lifts": [{
            "lift_id": "L-01", "task_id": "T-01",
            "input": {"mass_kg": 100, "area_m2": 2, "wind_limit_ms": 10, "pickup": [1, 2, 82]},
        }],
        "findings": [{
            "rule_id": "edge_work_requires_guardrail", "source": "TEST-PPR",
            "start": "2026-11-16 07:00", "end": "2026-11-16 07:10", "zone_ids": ["Z-EDGE"],
        }],
    }


def test_usd_layers_and_manifest(tmp_path):
    source = tmp_path / "scene_bundle.json"
    source.write_text(json.dumps(_bundle()), encoding="utf-8")
    result = export_openusd(source, tmp_path / "usd")
    stage = result["stage"].read_text(encoding="utf-8")
    assert stage.index("@animation.usda@") < stage.index("@risks.usda@") < stage.index("@geometry.usda@")
    assert "float[] primvars:displayOpacity" in result["layers"]["geometry"].read_text(encoding="utf-8")
    manifest = json.loads(result["manifest"].read_text(encoding="utf-8"))
    assert len(manifest["source_sha256"]) == 64
    assert manifest["counts"] == {"timeline_frames": 2, "zones": 2, "crews": 1, "lifts": 1, "findings": 1}
    assert manifest["time_codes_per_second"] == 24


def _blender() -> str:
    candidate = os.environ.get("BLENDER") or str(Path.home() / "Library/Application Support/Steam/steamapps/common/Blender/Blender.app/Contents/MacOS/Blender")
    if not Path(candidate).is_file():
        pytest.skip("Blender с pxr не установлен")
    return candidate


def test_usd_flatten_and_blender_animation(tmp_path):
    blender = _blender()
    source = tmp_path / "scene_bundle.json"
    source.write_text(json.dumps(_bundle()), encoding="utf-8")
    result = export_openusd(source, tmp_path / "usd", flatten=True, blender_path=blender)
    validation = json.loads(result["flatten_validation"].read_text(encoding="utf-8"))
    assert validation["animated_prim_count"] >= 5
    assert "/Site/Crane/Slew" in validation["animated_prims"]
    assert "/Site/Crews/C1" in validation["animated_prims"]
    assert "/Site/Loads/L_01" in validation["animated_prims"]
    assert "/Site/Risks/Z_EDGE/Hazard" in validation["animated_prims"]

    expression = (
        "import bpy; bpy.ops.wm.read_factory_settings(use_empty=True); "
        f"bpy.ops.wm.usd_import(filepath={str(result['flattened_stage'])!r}, merge_parent_xform=False, import_defined_only=False); "
        "cached=[o for o in bpy.data.objects if any(c.type=='TRANSFORM_CACHE' for c in o.constraints)]; "
        "assert len(cached) >= 5, len(cached); print('USD_TEST_OK', len(cached))"
    )
    completed = subprocess.run(
        [blender, "--factory-startup", "-b", "--python-exit-code", "1", "--python-expr", expression],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "USD_TEST_OK" in completed.stdout


def test_usd_ifc_mesh_geometry(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    bundle = _bundle()
    mesh = trimesh.creation.box(extents=[4, 0.2, 3])
    mesh.apply_translation([30, 20, 246])
    mesh.metadata = {"global_id": "TEST-WALL", "ifc_type": "IfcWall"}
    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name="TEST-WALL", geom_name="WallGeom")
    scene.export(tmp_path / "model.glb")
    bundle["model"] = {
        "source_file": "verification.ifc",
        "source_sha256": "verification-source",
        "glb_file": "model.glb",
        "glb_sha256": hashlib.sha256((tmp_path / "model.glb").read_bytes()).hexdigest(),
        "glb_coordinates": "ifc_coordinates_preserved",
    }
    source = tmp_path / "scene_bundle.json"
    source.write_text(json.dumps(bundle), encoding="utf-8")
    result = export_openusd(source, tmp_path / "usd", flatten=True, blender_path=_blender())

    assert result["layers"]["ifc_mesh"].is_file()
    stage = result["stage"].read_text(encoding="utf-8")
    assert stage.index("@ifc_mesh.usda@") < stage.index("@geometry.usda@")
    assert "Slab_L1" not in result["layers"]["geometry"].read_text(encoding="utf-8")
    manifest = json.loads(result["manifest"].read_text(encoding="utf-8"))
    assert manifest["counts"]["mesh_count"] == 1
    assert manifest["counts"]["triangle_count"] == 12
    assert "IFC-треугольники преобразованы" in manifest["geometry_note"]

    expression = (
        "from pxr import Usd; "
        f"s=Usd.Stage.Open({str(result['flattened_stage'])!r}); "
        "p=s.GetPrimAtPath('/Site/IFC_Model/TEST_WALL/Geometry'); "
        "points=p.GetAttribute('points').Get(); "
        "assert len(points)==8; "
        "assert len(p.GetAttribute('faceVertexCounts').Get())==12; "
        "assert len(p.GetAttribute('faceVertexIndices').Get())==36; "
        "low=[min(v[i] for v in points) for i in range(3)]; "
        "high=[max(v[i] for v in points) for i in range(3)]; "
        "assert all(abs(a-b)<1e-4 for a,b in zip(low,[28,19.9,244.5])); "
        "assert all(abs(a-b)<1e-4 for a,b in zip(high,[32,20.1,247.5])); "
        "import bpy; bpy.ops.wm.read_factory_settings(use_empty=True); "
        f"bpy.ops.wm.usd_import(filepath={str(result['flattened_stage'])!r}, merge_parent_xform=False, import_defined_only=False); "
        "assert any(o.name=='Geometry' for o in bpy.data.objects); "
        "print('IFC_USD_GEOMETRY_OK')"
    )
    completed = subprocess.run(
        [_blender(), "--factory-startup", "-b", "--python-exit-code", "1", "--python-expr", expression],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "IFC_USD_GEOMETRY_OK" in completed.stdout
