"""Тесты экспорта scene_bundle → OpenUSD stage (risk_sim.usd_stage)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from risk_sim.usd_stage import export_stage, validate_stage

CRANE = {"mast": {"x": 66.0, "y": 20.0, "base_z": 0.0, "top_z": 292.0, "section_m": 2.4},
         "jib": {"length_m": 60.0, "counter_jib_m": 18.0, "head_z": 288.0}}


def _mini_bundle(tmp_path: Path) -> Path:
    frames = [
        {"t": "2026-11-16 07:00", "crane": None, "loads": [], "tasks_active": []},
        {"t": "2026-11-16 07:10", "crane": {"slew_deg": 150.0, "radius_m": 60.0,
                                            "hook_z_m": 99.0, "jib_head_z_m": 288.0,
                                            "mast_x": 66.0, "mast_y": 20.0},
         "loads": [{"lift_id": "L-01", "load_name": "Щит опалубки", "mass_kg": 450.0,
                    "phase": "hoist", "x": 14.0, "y": 50.0, "z": 83.0}],
         "tasks_active": ["T-01"]},
    ]
    bundle = {
        "timeline": {
            "meta": {"project": "Тест stage", "t_start": "2026-11-16 07:00",
                     "t_end": "2026-11-16 07:10"},
            "crane": CRANE,
            "zones": [{"id": "Z1", "level": "L62", "type": "work", "x_min": 10.0,
                       "y_min": 45.0, "x_max": 20.0, "y_max": 55.0,
                       "z_floor": 244.0, "z_top": 248.0}],
            "crews": [{"crew_id": "C1", "name": "Бригада 1", "size": 2, "color": "#3b82f6"}],
            "frames": frames,
        },
        "lifts": [{"lift_id": "L-01", "load_name": "Щит опалубки", "input": {"area_m2": 3.0}}],
        "clearances": {"margin_m": 0.5, "index_mode": "zones_aabb", "model_file": None,
                       "per_frame": [{"i": 1, "t": frames[1]["t"], "lift_id": "L-01",
                                      "min_dist_m": 0.4, "component": "load",
                                      "point": [14.0, 50.0, 83.0], "status": "warn"}],
                       "events": []},
        "agents": {"tracks": [{"crew_id": "C1",
                               "points": [{"t": "2026-11-16 07:10", "x": 30.0, "y": 30.0,
                                           "z": 0.0, "state": "working"}]}]},
    }
    path = tmp_path / "scene_bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return path


def test_export_stage_creates_stage(tmp_path: Path):
    bundle = _mini_bundle(tmp_path)
    out = tmp_path / "scene.usda"
    info = export_stage(bundle, out, fps=24.0)
    assert out.is_file()
    assert info["frames"] == 2
    assert info["kinds"] == ["panel"]
    text = out.read_text(encoding="utf-8")
    assert 'upAxis = "Z"' in text
    assert "metersPerUnit = 1" in text
    assert 'Xform "Slew"' in text
    assert 'Xform "Load_panel"' in text
    assert 'Mesh "C0000"' in text
    assert 'Capsule "C1_00"' in text


def test_validate_stage_pass(tmp_path: Path):
    bundle = _mini_bundle(tmp_path)
    out = tmp_path / "scene.usda"
    export_stage(bundle, out)
    report = validate_stage(bundle, out)
    assert report["verdict"] == "PASS"
    assert report["worst_slew_deg"] <= 0.5
    assert report["worst_load_m"] <= 0.05
    assert (tmp_path / "scene_stage_validation.json").is_file()


def test_validate_stage_detects_tampering(tmp_path: Path):
    bundle = _mini_bundle(tmp_path)
    out = tmp_path / "scene.usda"
    export_stage(bundle, out)
    # подделываем угол стрелы через pxr API и пересохраняем
    from pxr import Gf, Usd, UsdGeom
    stage = Usd.Stage.Open(str(out))
    slew = stage.GetPrimAtPath("/RiskSim/Crane/Slew")
    for op in UsdGeom.Xformable(slew).GetOrderedXformOps():
        if op.GetOpName() == "xformOp:rotateXYZ":
            op.Set(Gf.Vec3f(0.0, 0.0, 90.0), Usd.TimeCode(1))
    stage.GetRootLayer().Save()
    report = validate_stage(bundle, out, sample_step=1)
    assert report["verdict"] == "FAIL"
    assert report["worst_slew_deg"] > 0.5
