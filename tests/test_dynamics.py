from __future__ import annotations

import numpy as np
import pytest

from core.physics.pendulum import simulate_pendulum


def test_collision_has_force_and_metadata():
    result = simulate_pendulum(
        lambda _: 0.0, duration_s=1.0, mass_kg=1800, length_m=12, area_m2=0,
        anchor=(0, 0, 30), timestep=0.01, sample_every_s=0.1, engine="mujoco",
        collision_boxes=[{"name": "Slab_L1", "center": [0, 0, 18], "size": [5, 5, 0.5]}],
    )
    assert result.meta["collision_count"] == 1
    assert result.collisions[0]["name"] == "Slab_L1"
    assert result.collisions[0]["max_normal_force_N"] > 0
    assert result.collisions[0]["max_depth_m"] > 0
    assert result.meta["contact_max_normal_force_N"] > 0


def test_distant_obstacle_has_no_collision():
    result = simulate_pendulum(
        lambda _: 0.0, duration_s=1.0, mass_kg=1800, length_m=12, area_m2=0,
        anchor=(0, 0, 30), timestep=0.01, sample_every_s=0.1, engine="mujoco",
        collision_boxes=[{"name": "Distant", "center": [0, 0, 60], "size": [5, 5, 0.5]}],
    )
    assert result.meta["collision_count"] == 0
    assert result.collisions == []


def test_moving_anchor_follows_trajectory():
    result = simulate_pendulum(
        lambda _: 0.0, duration_s=2.0, mass_kg=1800, length_m=12, area_m2=0,
        anchor=(0, 0, 30), timestep=0.01, sample_every_s=0.1, engine="mujoco",
        anchor_fn=lambda t: (t, 0, 30),
    )
    assert result.meta["moving_anchor"] is True
    assert result.anchor_track is not None
    assert result.anchor_track[-1][0] > 1.9
    assert result.pos[-1][0] > 1.8
    assert result.track_json()[-1]["anchor_pos"][0] >= 1.9


def test_analytic_rejects_full_dynamics():
    with pytest.raises(ValueError, match="mujoco"):
        simulate_pendulum(
            lambda _: 0.0, duration_s=1.0, mass_kg=1800, length_m=12, area_m2=0,
            anchor=(0, 0, 30), engine="analytic", anchor_fn=lambda t: (t, 0, 30),
        )


def test_full_operation_trajectory_and_collision_report():
    import copy
    from dataclasses import replace
    import pandas as pd
    from core.context import build_context
    from core.sim.lifts import _run_swing

    ctx = build_context("data/sample")
    lift = ctx.ds.lifts[0]
    task = replace(ctx.ds.task_by_id[lift.task_id], end=ctx.ds.task_by_id[lift.task_id].start + pd.Timedelta(minutes=1))
    cfg = copy.copy(ctx.cfg)
    cfg.physics = {
        **cfg.physics,
        "moving_anchor": True,
        "collision_geometry": True,
        "moving_timestep_s": 0.05,
        "moving_sample_s": 1.0,
    }
    result = _run_swing(lift, task, ctx.wind, cfg, 1000.0, engine="mujoco", zones=ctx.ds.zones)
    assert result["moving_anchor"] is True
    assert result["window_s"] == 60.0
    assert result["collision_boxes"] > 0
    assert "collisions" in result
    assert result["measured_period_s"] is None
    assert result["track"][0]["anchor_pos"] != result["track"][-1]["anchor_pos"]
