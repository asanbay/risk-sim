"""Тесты клиренса грузового пути (risk_sim.clearance)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from risk_sim.clearance import (compute_clearance, events_to_findings,
                                frame_sample_points, load_half_dims,
                                obstacles_from_glb, obstacles_from_zones)

CRANE = {"mast": {"x": 66.0, "y": 20.0, "base_z": 0.0, "top_z": 292.0, "section_m": 2.4},
         "jib": {"length_m": 60.0, "counter_jib_m": 18.0, "head_z": 288.0}}


def _frame(t: str, x: float, y: float, z: float, slew: float = 150.0, radius: float = 50.0,
           hook_z: float | None = None, lift_id: str = "L-01") -> dict:
    if hook_z is None:
        hook_z = z + 20.0
    return {
        "t": t,
        "crane": {"slew_deg": slew, "radius_m": radius, "hook_z_m": hook_z,
                  "jib_head_z_m": 288.0, "mast_x": 66.0, "mast_y": 20.0},
        "loads": [{"lift_id": lift_id, "load_name": "Щит опалубки", "mass_kg": 450.0,
                   "phase": "travel", "x": x, "y": y, "z": z}],
    }


ZONES = [
    {"id": "WALL-1", "level": "L62", "type": "work",
     "x_min": 10.0, "y_min": 45.0, "x_max": 20.0, "y_max": 55.0,
     "z_floor": 244.0, "z_top": 248.0},
]
LIFTS = {"L-01": {"load_name": "Щит опалубки", "area_m2": 3.0}}


def test_load_half_dims_heuristic():
    panel = load_half_dims({"load_name": "Щит опалубки стальной", "area_m2": 3.0})
    assert panel == pytest.approx((1.2247, 0.6124, 1.25), abs=1e-3)
    rebar = load_half_dims({"load_name": "Арматура A500 d=20", "area_m2": 3.0})
    assert rebar == (6.0, 0.40, 0.40)


def test_frame_sample_points_components():
    frame = _frame("2026-11-16 10:00", 20.0, 50.0, 100.0)
    pts, labels = frame_sample_points(frame, CRANE, (1.2, 0.6, 1.25))
    assert len(pts) == len(labels)
    for component in ("mast", "jib", "hook", "rope", "load", "sling"):
        assert component in labels
    # тележка на радиусе 50 при slew 150°
    idx = labels.index("hook")
    assert abs(pts[idx][0] - (66.0 + 50.0 * np.cos(np.radians(150.0)))) < 1e-6


def test_zone_obstacles_violation_and_ok():
    frames = [
        _frame("2026-11-16 09:00", 15.0, 50.0, 246.0),   # груз внутри зоны-препятствия
        _frame("2026-11-16 10:00", 40.0, 50.0, 246.0),   # далеко от препятствия
    ]
    result = compute_clearance(frames, CRANE, LIFTS, margin_m=0.5, zones=ZONES)
    assert result.checked_frames == 2
    assert result.per_frame[0]["status"] == "violation"
    assert result.per_frame[0]["min_dist_m"] == 0.0
    assert result.per_frame[1]["status"] == "ok"
    assert len(result.events) == 1
    assert result.events[0]["status"] == "critical"


def test_events_to_findings_fields():
    frames = [_frame("2026-11-16 09:00", 15.0, 50.0, 246.0)]
    result = compute_clearance(frames, CRANE, LIFTS, margin_m=0.5, zones=ZONES)

    class FakeZone:
        def __init__(self, d):
            self.__dict__.update(d)

    class FakeDS:
        zones = [FakeZone(z) for z in ZONES]
        lifts = []

    findings = events_to_findings(result, FakeDS())
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "load_path_clearance"
    assert f.severity == "critical"
    assert f.metrics["min_distance_m"] == 0.0
    assert f.lift_id == "L-01"


def test_glb_obstacles(tmp_path: Path):
    # стена 10×6 м на пути груза
    wall = trimesh.creation.box(extents=(0.5, 6.0, 6.0))
    wall.apply_translation((15.0, 50.0, 246.0))
    glb = tmp_path / "model.glb"
    wall.export(glb)
    obstacles, info = obstacles_from_glb(glb)
    assert obstacles.triangles == 12
    assert info["index"] == "kdtree"  # rtree отсутствует

    frames = [
        _frame("2026-11-16 09:00", 15.0, 50.0, 246.0),   # сквозь стену
        _frame("2026-11-16 10:00", 40.0, 20.0, 246.0),   # в 30+ м
    ]
    result = compute_clearance(frames, CRANE, LIFTS, margin_m=0.5, zones=None,
                               model_path=glb)
    assert result.model_file == "model.glb"
    assert result.per_frame[0]["min_dist_m"] < 0.5
    assert result.per_frame[1]["min_dist_m"] > 5.0


def test_no_loads_no_crane_frames_skipped():
    frames = [
        {"t": "2026-11-16 07:00", "crane": None, "loads": []},
        {"t": "2026-11-16 07:10", "crane": {"slew_deg": 0, "radius_m": 20, "hook_z_m": 100,
                                            "jib_head_z_m": 288, "mast_x": 66, "mast_y": 20},
         "loads": []},
    ]
    result = compute_clearance(frames, CRANE, LIFTS, margin_m=0.5, zones=ZONES)
    assert result.checked_frames == 0
    assert result.events == []
