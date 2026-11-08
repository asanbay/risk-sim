from __future__ import annotations

from typing import Callable

import numpy as np

from .physics import wind_force

ENGINE = "mujoco_hinge_rk4"


def integrate_pendulum(
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
    try:
        import mujoco
    except ImportError as error:
        raise RuntimeError("Для physics.engine=mujoco_hinge установите пакет: pip install -e '.[physics]'") from error

    steps = int(round(duration_s / timestep_s))
    if steps <= 0 or not np.isclose(steps * timestep_s, duration_s):
        raise ValueError("duration_s должно быть кратно timestep_s")
    joint_damping = damping_per_s * mass_kg * length_m**2
    xml = f"""
    <mujoco>
      <option timestep="{timestep_s}" gravity="0 0 {-gravity}" integrator="RK4"/>
      <worldbody>
        <body name="load" pos="0 0 {-length_m}">
          <joint name="swing" type="hinge" pos="0 0 {length_m}" axis="0 -1 0" damping="{joint_damping}"/>
          <geom name="point_mass" type="sphere" size="0.05" mass="{mass_kg}"/>
        </body>
      </worldbody>
    </mujoco>
    """
    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    body_id = model.body("load").id
    data.qpos[0] = theta0
    mujoco.mj_forward(model, data)

    times = np.linspace(0.0, duration_s, steps + 1)
    theta = np.zeros(steps + 1)
    omega = np.zeros(steps + 1)
    theta[0] = theta0
    omega[0] = data.qvel[0]
    for index in range(steps):
        angle = float(data.qpos[0])
        displacement = length_m * np.sin(angle)
        demand = tagline_stiffness * abs(displacement)
        tagline = -np.sign(displacement) * min(demand, tagline_capacity) if tagline_stiffness else 0.0
        data.xfrc_applied[body_id, 0] = float(wind_force(wind_fn(times[index]), area_m2, cd, rho)) + tagline
        mujoco.mj_step(model, data)
        theta[index + 1] = data.qpos[0]
        omega[index + 1] = data.qvel[0]
    return times, theta, omega
