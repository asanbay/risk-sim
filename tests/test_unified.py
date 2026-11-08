from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from risk_sim.model import import_ifc_model, load_model_metadata
from risk_sim.physics import validate_physics
from risk_sim.scenario import run_simulation


ROOT = Path(__file__).resolve().parents[1]


def _ifc_wall(path: Path):
    pytest.importorskip("ifcopenshell", reason="IfcOpenShell не установлен")
    from ifcopenshell.api import context, geometry, project, root, spatial, unit
    model = project.create_file(version="IFC4")
    root.create_entity(model, ifc_class="IfcProject", name="Unified IFC verification")
    unit.assign_unit(model)
    model_context = context.add_context(model, context_type="Model")
    body = context.add_context(
        model, context_type="Model", context_identifier="Body",
        target_view="MODEL_VIEW", parent=model_context,
    )
    storey = root.create_entity(model, ifc_class="IfcBuildingStorey", name="Level 1")
    wall = root.create_entity(model, ifc_class="IfcWall", name="Wall 4000 x 3000 mm")
    spatial.assign_container(model, products=[wall], relating_structure=storey)
    representation = geometry.add_wall_representation(
        model, context=body, length=4000, height=3000, thickness=200,
    )
    geometry.assign_representation(model, product=wall, representation=representation)
    geometry.edit_object_placement(model, product=wall, matrix=np.eye(4), is_si=False)
    model.write(path)
    return path


def test_unified_mujoco_validation():
    from risk_sim.io import load_inputs
    inputs = load_inputs(ROOT / "data")
    result = validate_physics(inputs.rules["physics"])
    assert result["all_passed"] is True
    assert result["engine"] == "mujoco_hinge"
    comparison = result["checks"]["numpy_mujoco_variable_wind_comparison"]
    assert comparison["actual_max_abs_difference_rad"] <= comparison["tolerance_rad"]


def test_unified_fast_pipeline(tmp_path):
    result = run_simulation(ROOT / "data", tmp_path / "results", tmp_path / "viewer")
    scenario = json.loads(result["scenario"].read_text(encoding="utf-8"))
    assert scenario["metadata"]["software_version"] == "0.7.0"
    assert scenario["validation"]["all_passed"] is True
    assert scenario["lifts"][0]["summary"]["engine"] == "mujoco_hinge_rk4"
    assert result["report_docx"].is_file()
    assert result["report_pdf"].is_file()


def test_unified_ifc_units_and_pilot_metadata(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    source = _ifc_wall(tmp_path / "millimetres.ifc")
    data = tmp_path / "data"
    shutil.copytree(ROOT / "data/sample", data)
    metadata = import_ifc_model(source, data)
    assert metadata["unit_scale_to_m"] == 0.001
    assert metadata["triangle_count"] == 12
    assert metadata["failed_element_count"] == 0
    assert load_model_metadata(data)["glb_sha256"] == metadata["glb_sha256"]
    imported = trimesh.load(data / "model.glb", force="scene")
    assert np.allclose(imported.extents, [4.0, 0.2, 3.0], atol=1e-5)

    from risk_sim.pilot import main as pilot_main
    out = tmp_path / "pilot"
    return_code = pilot_main([
        "--data", str(data), "--out", str(out),
        "--mc-lifts", "20", "--mc-schedule", "0", "--skip-lift-swing",
        "--agent-dt", "60", "--timeline-dt", "60", "--no-report", "--no-viewer",
    ])
    assert return_code == 0
    bundle = json.loads((out / "scene_bundle.json").read_text(encoding="utf-8"))
    assert bundle["model"]["source_file"] == "millimetres.ifc"
    assert (out / "model.glb").is_file()


def test_unified_cli_has_both_pipelines():
    from risk_sim.cli import _parser
    parser = _parser()
    fast = parser.parse_args(["run", "--data", "data"])
    pilot = parser.parse_args(["pilot", "--data", "data/sample", "--swing-engine", "mujoco"])
    assert fast.command == "run"
    assert pilot.command == "pilot"
    assert pilot.output == "results/pilot"
    assert pilot.swing_engine == "mujoco"
