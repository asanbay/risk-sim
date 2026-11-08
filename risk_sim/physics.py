from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd


def wind_force(v_ms: float | np.ndarray, area_m2: float | np.ndarray, cd: float | np.ndarray, rho: float = 1.225) -> float | np.ndarray:
    return 0.5 * rho * cd * area_m2 * np.asarray(v_ms) ** 2


def sling_tension(mass_kg: float | np.ndarray, carrying_legs: int, angle_deg: float, gravity: float = 9.80665) -> float | np.ndarray:
    return np.asarray(mass_kg) * gravity / (carrying_legs * np.sin(np.radians(angle_deg)))


def wind_speed_for_force(force_n: float, area_m2: float, cd: float, rho: float) -> float:
    return float(np.sqrt(2.0 * force_n / (rho * cd * area_m2)))


def _integrate_pendulum(
    length_m: float,
    mass_kg: float,
    timestep_s: float,
    duration_s: float,
    damping_per_s: float,
    gravity: float,
    wind_fn: Callable[[float], float],
    area_m2: float,
    cd: float,
    rho: float,
    tagline_stiffness: float,
    tagline_capacity: float,
    theta0: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    steps = int(round(duration_s / timestep_s))
    if steps <= 0 or not np.isclose(steps * timestep_s, duration_s):
        raise ValueError("duration_s должно быть кратно timestep_s")
    times = np.linspace(0.0, duration_s, steps + 1)
    theta = np.zeros(steps + 1)
    omega = np.zeros(steps + 1)
    theta[0] = theta0

    def acceleration(t: float, angle: float, speed: float) -> float:
        displacement = length_m * np.sin(angle)
        demand = tagline_stiffness * abs(displacement)
        tagline = -np.sign(displacement) * min(demand, tagline_capacity) if tagline_stiffness else 0.0
        horizontal_force = wind_force(wind_fn(t), area_m2, cd, rho) + tagline
        return float(
            -(gravity / length_m) * np.sin(angle)
            + horizontal_force / (mass_kg * length_m) * np.cos(angle)
            - damping_per_s * speed
        )

    for index in range(steps):
        t = times[index]
        angle, speed = theta[index], omega[index]
        dt = timestep_s
        a1 = acceleration(t, angle, speed)
        a2 = acceleration(t + dt / 2, angle + speed * dt / 2, speed + a1 * dt / 2)
        a3 = acceleration(t + dt / 2, angle + (speed + a1 * dt / 2) * dt / 2, speed + a2 * dt / 2)
        a4 = acceleration(t + dt, angle + (speed + a2 * dt / 2) * dt, speed + a3 * dt / 2)
        theta[index + 1] = angle + dt * (speed + 2 * (speed + a1 * dt / 2) + 2 * (speed + a2 * dt / 2) + (speed + a3 * dt / 2)) / 6
        omega[index + 1] = speed + dt * (a1 + 2 * a2 + 2 * a3 + a4) / 6
    return times, theta, omega


def simulate_lift(lift: pd.Series, task: pd.Series, wind: pd.DataFrame, physics: dict[str, Any]) -> dict[str, Any]:
    rho = float(physics.get("air_density_kg_m3", 1.225))
    gravity = float(physics.get("gravity_m_s2", 9.80665))
    timestep = float(physics.get("timestep_s", 0.02))
    duration = min(float(physics.get("duration_s", 300.0)), float((task.end - task.start).total_seconds()))
    output_step = float(physics.get("output_step_s", 0.1))
    metric = physics.get("wind_metric", "gust_ms")
    wind_seconds = (wind.timestamp - task.start).dt.total_seconds().to_numpy(dtype=float)
    wind_values = wind[metric].to_numpy(dtype=float)

    def wind_at(t: float) -> float:
        return float(np.interp(t, wind_seconds, wind_values))

    length = float(lift.get("rope_length_m", 12.0))
    stiffness = float(lift.get("tagline_stiffness_n_per_m", 0.0))
    capacity = float(lift.get("tagline_capacity_n", np.inf))
    engine = str(physics.get("engine", "numpy_nonlinear_pendulum_rk4"))
    if engine == "numpy_nonlinear_pendulum_rk4":
        integrate = _integrate_pendulum
        engine_name = engine
    elif engine == "mujoco_hinge":
        from .mujoco_physics import ENGINE, integrate_pendulum
        integrate = integrate_pendulum
        engine_name = ENGINE
    else:
        raise ValueError(f"Неизвестный physics.engine: {engine}")
    times, theta, omega = integrate(
        length, float(lift.mass_kg), timestep, duration,
        float(physics.get("damping_per_s", 0.08)), gravity, wind_at,
        float(lift.area_m2), float(lift.cd), rho, stiffness, capacity,
    )
    record_every = int(round(output_step / timestep))
    if record_every < 1 or not np.isclose(record_every * timestep, output_step):
        raise ValueError("output_step_s должен быть положительным и кратен timestep_s")
    indices = np.unique(np.append(np.arange(0, len(times), record_every), len(times) - 1))
    direction = np.radians(float(physics.get("wind_direction_deg", 0.0)))
    wind_unit = np.array([np.cos(direction), np.sin(direction)])
    slew = np.radians(float(lift.get("slew_angle_deg", 0.0)))
    pivot = np.array([
        float(lift.get("crane_x_m", 0.0)) + float(lift.radius_m) * np.cos(slew),
        float(lift.get("crane_y_m", 0.0)) + float(lift.radius_m) * np.sin(slew),
        float(lift.hook_height_m) + length,
    ])
    track = []
    for index in indices:
        angle = float(theta[index])
        horizontal = length * np.sin(angle)
        position = np.array([
            pivot[0] + wind_unit[0] * horizontal,
            pivot[1] + wind_unit[1] * horizontal,
            pivot[2] - length * np.cos(angle),
        ])
        speed = float(wind_at(times[index]))
        force = float(wind_force(speed, float(lift.area_m2), float(lift.cd), rho))
        demand = stiffness * abs(horizontal)
        half = angle / 2
        track.append({
            "t": round(float(times[index]), 6),
            "timestamp": (task.start + pd.to_timedelta(times[index], unit="s")).isoformat(),
            "pos": [round(float(value), 6) for value in position],
            "quat": [float(np.cos(half)), float(wind_unit[1] * np.sin(half)), float(-wind_unit[0] * np.sin(half)), 0.0],
            "swing_deg": float(np.degrees(angle)),
            "wind_ms": speed,
            "wind_force_n": force,
            "tagline_demand_n": float(demand),
            "tagline_force_n": float(min(demand, capacity)),
        })

    tension = float(sling_tension(float(lift.mass_kg), int(lift.carrying_legs), float(lift.sling_angle_deg), gravity))
    wll_n = float(lift.sling_wll_kg) * gravity
    max_wind = max(point["wind_ms"] for point in track)
    max_force = max(point["wind_force_n"] for point in track)
    cd_min = float(lift.get("cd_min", lift.cd))
    cd_max = float(lift.get("cd_max", lift.cd))
    summary = {
        "lift_id": lift.lift_id,
        "task_id": lift.task_id,
        "engine": engine_name,
        "start": task.start.isoformat(),
        "duration_s": duration,
        "timestep_s": timestep,
        "pivot_m": pivot.tolist(),
        "rope_length_m": length,
        "max_wind_ms": max_wind,
        "wind_limit_ms": float(lift.wind_limit_ms),
        "max_wind_force_n": max_force,
        "max_abs_swing_deg": float(np.max(np.abs(np.degrees(theta)))),
        "max_horizontal_displacement_m": float(np.max(np.abs(length * np.sin(theta)))),
        "max_tagline_demand_n": float(np.max(stiffness * np.abs(length * np.sin(theta)))),
        "quasi_static_tagline_force_max_n": max_force,
        "tagline_capacity_n": capacity,
        "sling_tension_per_leg_n": tension,
        "sling_wll_per_leg_n": wll_n,
        "sling_utilization": tension / wll_n,
        "static_deflection_at_max_wind_deg": float(np.degrees(np.arctan(max_force / (float(lift.mass_kg) * gravity)))),
        "tagline_wind_capacity_ms_by_cd": {
            "cd_min": wind_speed_for_force(capacity, float(lift.area_m2), cd_min, rho),
            "cd_nominal": wind_speed_for_force(capacity, float(lift.area_m2), float(lift.cd), rho),
            "cd_max": wind_speed_for_force(capacity, float(lift.area_m2), cd_max, rho),
        },
        "within_wind_limit": max_wind <= float(lift.wind_limit_ms),
        "within_static_sling_wll": tension <= wll_n,
        "within_quasi_static_tagline_capacity": max_force <= capacity,
        "load_dimensions_m": [
            float(lift.get("load_length_m", 2.0)),
            float(lift.get("load_width_m", 1.0)),
            float(lift.get("load_height_m", 1.0)),
        ],
    }
    return {"summary": summary, "track": track}


def _pendulum_period(times: np.ndarray, theta: np.ndarray) -> float:
    crossings = []
    for index in range(1, len(theta)):
        if (theta[index - 1] <= 0 < theta[index]) or (theta[index - 1] >= 0 > theta[index]):
            fraction = -theta[index - 1] / (theta[index] - theta[index - 1])
            crossings.append(times[index - 1] + fraction * (times[index] - times[index - 1]))
    if len(crossings) < 3:
        raise RuntimeError("Не удалось измерить период маятника")
    return float(2 * np.mean(np.diff(crossings)))


def validate_physics(physics: dict[str, Any]) -> dict[str, Any]:
    rho = float(physics.get("air_density_kg_m3", 1.225))
    gravity = float(physics.get("gravity_m_s2", 9.80665))
    selected_engine = str(physics.get("engine", "numpy_nonlinear_pendulum_rk4"))
    if selected_engine not in {"numpy_nonlinear_pendulum_rk4", "mujoco_hinge"}:
        raise ValueError(f"Неизвестный physics.engine: {selected_engine}")
    measured_force = float(wind_force(12.0, 18.0, 1.2, rho))
    expected_force = 0.5 * rho * 1.2 * 18.0 * 12.0**2
    measured_tension = float(sling_tension(4000.0, 2, 45.0, gravity))
    expected_tension = 4000.0 * gravity / (2 * np.sin(np.pi / 4))

    length = 12.0
    small_angle_args = (
        length, 4000.0, 0.002, 10.0, 0.0, gravity,
        lambda _: 0.0, 0.0, 0.0, rho, 0.0, 0.0,
    )
    times, theta, omega = _integrate_pendulum(*small_angle_args, theta0=1e-5)
    measured_period = _pendulum_period(times, theta)
    expected_period = float(2 * np.pi * np.sqrt(length / gravity))

    constant_force = 1000.0
    constant_wind = wind_speed_for_force(constant_force, 18.0, 1.2, rho)
    static_args = (
        length, 4000.0, 0.01, 80.0, 0.8, gravity,
        lambda _: constant_wind, 18.0, 1.2, rho, 0.0, 0.0,
    )
    _, static_theta, _ = _integrate_pendulum(*static_args)
    measured_static = float(static_theta[-1])
    expected_static = float(np.arctan(constant_force / (4000.0 * gravity)))
    checks = {
        "wind_force": {"expected_n": expected_force, "actual_n": measured_force, "tolerance_n": 1e-9},
        "sling_tension": {"expected_n": expected_tension, "actual_n": measured_tension, "tolerance_n": 1e-9},
        "pendulum_small_angle_period": {
            "expected_s": expected_period, "actual_s": measured_period,
            "relative_tolerance": 0.005, "initial_angle_rad": 1e-5,
        },
        "pendulum_static_deflection": {
            "expected_rad": expected_static, "actual_rad": measured_static,
            "tolerance_rad": 0.001, "constant_wind_force_n": constant_force,
        },
    }
    checks["wind_force"]["pass"] = abs(measured_force - expected_force) <= checks["wind_force"]["tolerance_n"]
    checks["sling_tension"]["pass"] = bool(abs(measured_tension - expected_tension) <= checks["sling_tension"]["tolerance_n"])
    checks["pendulum_small_angle_period"]["pass"] = abs(measured_period / expected_period - 1) <= 0.005
    checks["pendulum_static_deflection"]["pass"] = abs(measured_static - expected_static) <= 0.001
    engines = ["numpy_nonlinear_pendulum_rk4"]

    if selected_engine == "mujoco_hinge":
        from .mujoco_physics import ENGINE, integrate_pendulum
        mujoco_times, mujoco_theta, _ = integrate_pendulum(*small_angle_args, theta0=1e-5)
        mujoco_period = _pendulum_period(mujoco_times, mujoco_theta)
        _, mujoco_static_theta, _ = integrate_pendulum(*static_args)
        mujoco_static = float(mujoco_static_theta[-1])
        comparison_args = (
            length, 4000.0, 0.002, 20.0, 0.08, gravity,
            lambda t: 9.0 + 2.0 * np.sin(0.7 * t), 18.0, 1.2, rho, 120.0, 1500.0,
        )
        _, numpy_comparison, _ = _integrate_pendulum(*comparison_args)
        _, mujoco_comparison, _ = integrate_pendulum(*comparison_args)
        comparison_tolerance = 0.002
        comparison_difference = float(np.max(np.abs(numpy_comparison - mujoco_comparison)))
        checks.update({
            "mujoco_pendulum_small_angle_period": {
                "expected_s": expected_period, "actual_s": mujoco_period,
                "relative_tolerance": 0.005, "initial_angle_rad": 1e-5,
                "pass": abs(mujoco_period / expected_period - 1) <= 0.005,
            },
            "mujoco_pendulum_static_deflection": {
                "expected_rad": expected_static, "actual_rad": mujoco_static,
                "tolerance_rad": 0.001, "constant_wind_force_n": constant_force,
                "pass": abs(mujoco_static - expected_static) <= 0.001,
            },
            "numpy_mujoco_variable_wind_comparison": {
                "actual_max_abs_difference_rad": comparison_difference,
                "tolerance_rad": comparison_tolerance,
                "duration_s": 20.0, "timestep_s": 0.002,
                "pass": comparison_difference <= comparison_tolerance,
            },
        })
        engines.append(ENGINE)
    return {
        "engine": selected_engine,
        "engines": engines,
        "all_passed": all(check["pass"] for check in checks.values()),
        "checks": checks,
    }


def sensitivity_analysis(lift: pd.Series, summary: dict[str, Any], physics: dict[str, Any]) -> list[dict[str, Any]]:
    gravity = float(physics.get("gravity_m_s2", 9.80665))
    rho = float(physics.get("air_density_kg_m3", 1.225))
    rows = []
    for parameter in ("cd", "mass_kg", "sling_angle_deg", "rope_length_m"):
        for factor in (0.7, 0.9, 1.1, 1.3):
            cd = float(lift.cd) * (factor if parameter == "cd" else 1.0)
            mass = float(lift.mass_kg) * (factor if parameter == "mass_kg" else 1.0)
            angle = float(lift.sling_angle_deg) * (factor if parameter == "sling_angle_deg" else 1.0)
            length = float(lift.rope_length_m) * (factor if parameter == "rope_length_m" else 1.0)
            force = float(wind_force(summary["max_wind_ms"], float(lift.area_m2), cd, rho))
            tension = float(sling_tension(mass, int(lift.carrying_legs), angle, gravity))
            utilization = tension / summary["sling_wll_per_leg_n"]
            within_tagline = force <= summary["tagline_capacity_n"]
            within_sling = utilization <= 1.0
            rows.append({
                "parameter": parameter,
                "factor": factor,
                "change_percent": round((factor - 1) * 100),
                "value": cd if parameter == "cd" else mass if parameter == "mass_kg" else angle if parameter == "sling_angle_deg" else length,
                "max_wind_force_n": force,
                "sling_utilization": utilization,
                "static_deflection_at_max_wind_deg": float(np.degrees(np.arctan(force / (mass * gravity)))),
                "small_angle_period_s": float(2 * np.pi * np.sqrt(length / gravity)),
                "tagline_wind_capacity_ms": wind_speed_for_force(summary["tagline_capacity_n"], float(lift.area_m2), cd, rho),
                "within_quasi_static_tagline_capacity": within_tagline,
                "within_static_sling_wll": within_sling,
                "threshold_conclusion_changed": (
                    within_tagline != summary["within_quasi_static_tagline_capacity"]
                    or within_sling != summary["within_static_sling_wll"]
                ),
                "outside_declared_cd_range": parameter == "cd" and not float(lift.cd_min) <= cd <= float(lift.cd_max),
            })
    return rows
