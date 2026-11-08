"""
core/physics/pendulum.py — груз на подвесе: маятник с ветром и оттяжкой.

Две независимые реализации, которые обязаны совпадать (проверка достоверности,
раздел 11):

1) АНАЛИТИЧЕСКАЯ (для быстрой проверки и Monte Carlo):
       период малых колебаний:      T0 = 2*pi*sqrt(L/g)
       статическое отклонение:      tan(theta) = F_wind / (m*g)
       усилие в подвесе:            T = m*g*cos(th) + F_wind*sin(th) + m*L*omega^2

2) MuJoCo (для видео и реалистичной раскачки):
       тело на сферическом шарнире, ветер как аэродинамическое сопротивление
       по ОТНОСИТЕЛЬНОЙ скорости  F = 0.5*rho*Cd*A*|v_rel|*v_rel,
       оттяжка как сила людей, ограниченная их возможностью.

Аэродинамика по относительной скорости — не украшение: именно она гасит
раскачку и задаёт установившееся отклонение, поэтому модель ведёт себя как
реальный груз, а не как вечный маятник.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np

G_DEFAULT = 9.80665


# ---------------------------------------------------------------------------
# аналитика
# ---------------------------------------------------------------------------
def pendulum_period(length_m: float, g: float = G_DEFAULT, amplitude_deg: float = 0.0) -> float:
    """Период колебаний маятника, с.

    При amplitude_deg > 0 вводится поправка на конечную амплитуду
    (первый член разложения):  T ≈ T0 * (1 + theta0^2/16).
    """
    L = max(float(length_m), 1e-6)
    T0 = 2.0 * math.pi * math.sqrt(L / g)
    th0 = math.radians(float(amplitude_deg))
    return T0 * (1.0 + th0 ** 2 / 16.0)


def static_deflection_deg(wind_force_N: float, mass_kg: float, g: float = G_DEFAULT) -> float:
    """Статическое отклонение от вертикали при постоянном ветре, градусы."""
    w = float(mass_kg) * g
    if w <= 0:
        return 90.0
    return math.degrees(math.atan(float(wind_force_N) / w))


def static_offset_m(wind_force_N: float, mass_kg: float, length_m: float, g: float = G_DEFAULT) -> float:
    """Горизонтальное смещение груза при статическом отклонении, м."""
    th = math.radians(static_deflection_deg(wind_force_N, mass_kg, g))
    return float(length_m) * math.sin(th)


def suspension_tension_N(mass_kg: float, theta_deg: float, omega_rad_s: float,
                         length_m: float, wind_force_N: float = 0.0,
                         g: float = G_DEFAULT) -> float:
    """Усилие в подвесе (крюк/трос), Н. См. вывод в docstring модуля."""
    th = math.radians(float(theta_deg))
    return (float(mass_kg) * g * math.cos(th)
            + float(wind_force_N) * math.sin(th)
            + float(mass_kg) * float(length_m) * float(omega_rad_s) ** 2)


# ---------------------------------------------------------------------------
# MuJoCo
# ---------------------------------------------------------------------------
def build_pendulum_xml(mass_kg: float = 4000.0, length_m: float = 12.0,
                       size_m: Sequence[float] = (1.5, 0.1, 3.0),
                       anchor_z: float = 20.0, timestep: float = 0.002,
                       damping: float = 0.0, integrator: str = "RK4",
                       g: float = G_DEFAULT) -> str:
    """XML модели: точка подвеса на высоте anchor_z, груз на length_m ниже.

    Инерция тела задаётся как у однородного параллелепипеда тех же габаритов:
    это важно для реалистичного поворота панели на ветру.
    """
    hx, hy, hz = [float(s) for s in size_m]
    m = float(mass_kg)
    ix = m / 12.0 * ((2 * hy) ** 2 + (2 * hz) ** 2)
    iy = m / 12.0 * ((2 * hx) ** 2 + (2 * hz) ** 2)
    iz = m / 12.0 * ((2 * hx) ** 2 + (2 * hy) ** 2)
    body_z = anchor_z - float(length_m)
    return f"""
<mujoco model="lift_pendulum">
  <option timestep="{timestep}" gravity="0 0 -{g}" integrator="{integrator}"/>
  <worldbody>
    <body name="load" pos="0 0 {body_z:.6f}">
      <joint name="swing" type="ball" pos="0 0 {float(length_m):.6f}" damping="{damping}"/>
      <inertial pos="0 0 0" mass="{m:.6f}" diaginertia="{ix:.6f} {iy:.6f} {iz:.6f}"/>
      <geom name="load_geom" type="box" size="{hx} {hy} {hz}" rgba="0.85 0.55 0.15 1"
            contype="0" conaffinity="0"/>
    </body>
  </worldbody>
</mujoco>
"""


@dataclass
class SwingResult:
    """Результат симуляции раскачки."""
    t: np.ndarray                        # с
    pos: np.ndarray                      # (N,3) положение груза, м
    quat: np.ndarray                     # (N,4) ориентация
    theta_deg: np.ndarray                # отклонение от вертикали, град
    omega: np.ndarray                    # угловая скорость, рад/с
    wind_ms: np.ndarray                  # скорость ветра, м/с
    wind_force_N: np.ndarray             # сила ветра, Н
    tension_N: np.ndarray                # усилие в подвесе, Н
    tagline_force_N: np.ndarray          # фактическое усилие оттяжки, Н
    anchor: tuple[float, float, float] = (0.0, 0.0, 0.0)
    meta: dict = field(default_factory=dict)
    anchor_track: np.ndarray | None = None
    collisions: list[dict[str, Any]] = field(default_factory=list)

    # -- сводки ---------------------------------------------------------
    @property
    def max_theta_deg(self) -> float:
        return float(np.max(self.theta_deg)) if len(self.theta_deg) else 0.0

    def _horizontal_offsets(self) -> np.ndarray:
        if self.anchor_track is not None and len(self.anchor_track) == len(self.pos):
            return np.linalg.norm(self.pos[:, :2] - self.anchor_track[:, :2], axis=1)
        ax, ay, _ = self.anchor
        return np.hypot(self.pos[:, 0] - ax, self.pos[:, 1] - ay)

    @property
    def max_offset_m(self) -> float:
        return float(np.max(self._horizontal_offsets())) if len(self.pos) else 0.0

    @property
    def max_tension_N(self) -> float:
        return float(np.max(self.tension_N)) if len(self.tension_N) else 0.0

    @property
    def steady_offset_m(self) -> float:
        """Установившееся смещение (медиана второй половины записи)."""
        if not len(self.pos):
            return 0.0
        return float(np.median(self._horizontal_offsets()[len(self.pos) // 2:]))

    def signed_swing(self) -> np.ndarray | None:
        """ЗНАКОВОЕ отклонение вдоль оси с наибольшим размахом.

        Зачем: theta_deg — это модуль угла от вертикали, он всегда >= 0 и
        поэтому осциллирует с УДВОЕННОЙ частотой. Мерить по нему период —
        значит получить T/2. Знаковое смещение даёт честный период и
        одинаково определено для обоих движков (аналитика и MuJoCo).
        """
        if not len(self.pos):
            return None
        ax, ay, _ = self.anchor
        dx = self.pos[:, 0] - ax
        dy = self.pos[:, 1] - ay
        return dx if (np.ptp(dx) >= np.ptp(dy)) else dy

    def measured_period_s(self) -> float | None:
        """Период по переходам знакового смещения через среднее значение вверх."""
        sig = self.signed_swing()
        if sig is None or len(sig) < 10 or np.ptp(sig) < 1e-9:
            return None
        th = sig - np.mean(sig)
        crossings = []
        for i in range(1, len(th)):
            if th[i - 1] <= 0 < th[i]:
                # линейная интерполяция момента пересечения
                frac = -th[i - 1] / (th[i] - th[i - 1]) if th[i] != th[i - 1] else 0.0
                crossings.append(self.t[i - 1] + frac * (self.t[i] - self.t[i - 1]))
        if len(crossings) < 2:
            return None
        periods = np.diff(crossings)
        return float(np.mean(periods))

    def track_json(self, every: int = 1) -> list[dict]:
        """Формат для Blender/Three.js (совместим с разделом 8.3 спецификации)."""
        out = []
        for i in range(0, len(self.t), max(1, int(every))):
            record = {
                "t": round(float(self.t[i]), 4),
                "pos": [round(float(v), 4) for v in self.pos[i]],
                "quat": [round(float(v), 6) for v in self.quat[i]],
                "theta_deg": round(float(self.theta_deg[i]), 4),
                "wind_ms": round(float(self.wind_ms[i]), 3),
                "wind_force_N": round(float(self.wind_force_N[i]), 2),
                "tension_N": round(float(self.tension_N[i]), 2),
                "tagline_N": round(float(self.tagline_force_N[i]), 2),
            }
            if self.anchor_track is not None:
                record["anchor_pos"] = [round(float(v), 4) for v in self.anchor_track[i]]
            out.append(record)
        return out


def simulate_pendulum(wind_speed_fn: Callable[[float], float],
                      duration_s: float = 60.0,
                      mass_kg: float = 4000.0,
                      length_m: float = 12.0,
                      area_m2: float = 12.0,
                      cd: float = 1.2,
                      rho: float = 1.225,
                      anchor: Sequence[float] = (0.0, 0.0, 268.0),
                      wind_dir_deg: float = 0.0,
                      tagline_capacity_N: float = 0.0,
                      size_m: Sequence[float] = (1.5, 0.1, 3.0),
                      timestep: float = 0.002,
                      sample_every_s: float = 0.05,
                      initial_theta_deg: float = 0.0,
                      engine: str = "mujoco",
                      g: float = G_DEFAULT,
                      anchor_fn: Callable[[float], Sequence[float]] | None = None,
                      collision_boxes: Sequence[dict[str, Any]] = ()) -> SwingResult:
    """Симуляция груза на подвесе под ветром.

    wind_speed_fn(t) — скорость ветра, м/с (обычно интерполяция wind.csv).
    tagline_capacity_N — суммарное усилие людей на оттяжке; 0 = оттяжки нет.
    engine="mujoco" использует MuJoCo; "analytic" — одношаговую линейную
    модель (быстро, для Monte Carlo и для сверки).
    """
    ax, ay, az = [float(v) for v in anchor]
    wx = math.cos(math.radians(wind_dir_deg))
    wy = math.sin(math.radians(wind_dir_deg))

    if engine == "analytic":
        if anchor_fn is not None or collision_boxes:
            raise ValueError("moving_anchor и collision_geometry требуют engine='mujoco'")
        return _simulate_analytic(wind_speed_fn, duration_s, mass_kg, length_m, area_m2, cd, rho,
                                  (ax, ay, az), (wx, wy), tagline_capacity_N, timestep,
                                  sample_every_s, initial_theta_deg, g)
    return _simulate_mujoco(wind_speed_fn, duration_s, mass_kg, length_m, area_m2, cd, rho,
                            (ax, ay, az), (wx, wy), tagline_capacity_N, size_m, timestep,
                            sample_every_s, initial_theta_deg, g, anchor_fn, collision_boxes)


# -- MuJoCo ---------------------------------------------------------------
def build_dynamic_lift_xml(mass_kg, length_m, size_m, anchor, timestep, g,
                           collision_boxes=()):
    hx, hy, hz = [float(value) for value in size_m]
    mass = float(mass_kg)
    ix = mass / 12.0 * ((2 * hy) ** 2 + (2 * hz) ** 2)
    iy = mass / 12.0 * ((2 * hx) ** 2 + (2 * hz) ** 2)
    iz = mass / 12.0 * ((2 * hx) ** 2 + (2 * hy) ** 2)
    static = []
    for index, box in enumerate(collision_boxes):
        center = " ".join(f"{float(value):.6f}" for value in box["center"])
        size = " ".join(f"{float(value):.6f}" for value in box["size"])
        static.append(f'<geom name="Collision_{index}" type="box" pos="{center}" size="{size}"/>')
    return f"""
<mujoco model="dynamic_lift">
  <option timestep="{timestep}" gravity="0 0 -{g}" integrator="RK4"/>
  <worldbody>
    <body name="building" pos="0 0 0">
      {''.join(static)}
    </body>
    <body name="load" pos="{anchor[0]} {anchor[1]} {anchor[2] - length_m}">
      <joint name="swing" type="ball" pos="0 0 {length_m}" damping="0"/>
      <inertial pos="0 0 0" mass="{mass:.6f}" diaginertia="{ix:.6f} {iy:.6f} {iz:.6f}"/>
      <geom name="load_geom" type="box" size="{hx} {hy} {hz}" rgba="0.85 0.55 0.15 1"
            contype="1" conaffinity="1"/>
    </body>
  </worldbody>
</mujoco>
"""


def _simulate_mujoco(wind_speed_fn, duration_s, mass_kg, length_m, area_m2, cd, rho,
                     anchor, wdir, tagline_capacity_N, size_m, timestep, sample_every_s,
                     initial_theta_deg, g, anchor_fn=None, collision_boxes=()) -> SwingResult:
    import mujoco

    ax, ay, az = anchor
    xml = build_dynamic_lift_xml(mass_kg, length_m, size_m, anchor, timestep, g, collision_boxes)
    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    bid = model.body("load").id
    load_gid = model.geom("load_geom").id

    th0 = math.radians(initial_theta_deg)
    data.qpos[0:4] = [math.cos(th0 / 2.0), 0.0, -math.sin(th0 / 2.0), 0.0]
    model.body_pos[bid] = np.asarray(anchor, dtype=float) - np.array([0.0, 0.0, length_m])
    mujoco.mj_forward(model, data)

    sample_dt = max(timestep, float(sample_every_s))
    n_steps = int(round(duration_s / timestep))
    sample_every = max(1, int(round(sample_dt / timestep)))
    prev_pos = data.xpos[bid].copy()
    rec = []
    collisions = {}
    for k in range(n_steps):
        t = k * timestep
        anchor_now = np.asarray(anchor_fn(t) if anchor_fn else anchor, dtype=float)
        model.body_pos[bid] = anchor_now - np.array([0.0, 0.0, length_m])
        v_w = float(wind_speed_fn(t))

        p_now = data.xpos[bid]
        v_load = (p_now - prev_pos) / timestep if k > 0 else np.zeros(3)
        prev_pos = p_now.copy()
        vrel = np.array([v_w * wdir[0], v_w * wdir[1], 0.0]) - v_load
        speed = float(np.linalg.norm(vrel))
        F = 0.5 * rho * cd * area_m2 * speed * vrel if speed > 1e-9 else np.zeros(3)

        tag_mag = 0.0
        if tagline_capacity_N > 0:
            off = np.array([p_now[0] - anchor_now[0], p_now[1] - anchor_now[1]])
            d = float(np.linalg.norm(off))
            if d > 1e-6:
                tag_mag = float(min(tagline_capacity_N, 60.0 * d))
                F[0] -= tag_mag * off[0] / d
                F[1] -= tag_mag * off[1] / d

        data.xfrc_applied[bid, :3] = F
        mujoco.mj_step(model, data)

        for contact_index in range(data.ncon):
            contact = data.contact[contact_index]
            if load_gid not in (contact.geom1, contact.geom2):
                continue
            other = contact.geom2 if contact.geom1 == load_gid else contact.geom1
            name = model.geom(other).name
            index = int(name.removeprefix("Collision_")) if name.startswith("Collision_") else -1
            label = collision_boxes[index].get("name", name) if index >= 0 else name
            force = np.zeros(6)
            mujoco.mj_contactForce(model, data, contact_index, force)
            depth = max(0.0, -float(contact.dist))
            normal_force = abs(float(force[0]))
            if other not in collisions:
                collisions[other] = {
                    "geom": name, "name": label, "start_s": t, "end_s": t,
                    "max_depth_m": 0.0, "max_normal_force_N": 0.0,
                    "max_impact_speed_m_s": 0.0,
                    "last_position": contact.pos.tolist(), "normal": contact.frame[:3].tolist(),
                }
            event = collisions[other]
            event["end_s"] = t
            event["max_depth_m"] = max(event["max_depth_m"], depth)
            event["max_normal_force_N"] = max(event["max_normal_force_N"], normal_force)
            event["max_impact_speed_m_s"] = max(event["max_impact_speed_m_s"], float(np.linalg.norm(v_load)))
            event["last_position"] = contact.pos.tolist()
            event["normal"] = contact.frame[:3].tolist()

        if k % sample_every == 0:
            q = data.xquat[bid].copy()
            p = data.xpos[bid].copy()
            rod = np.array([p[0] - anchor_now[0], p[1] - anchor_now[1], p[2] - anchor_now[2]])
            nrm = float(np.linalg.norm(rod))
            horiz = math.hypot(rod[0], rod[1])
            mag = math.degrees(math.atan2(horiz, -rod[2])) if nrm > 1e-9 else 0.0
            theta = (1.0 if rod[0] >= 0 else -1.0) * mag
            omega = float(np.linalg.norm(data.qvel[0:3]))
            F_stat = 0.5 * rho * cd * area_m2 * v_w ** 2
            tension = suspension_tension_N(mass_kg, theta, omega, length_m, wind_force_N=F_stat, g=g)
            rec.append((t, p.tolist(), q.tolist(), theta, omega, v_w, F_stat, tension, tag_mag, anchor_now.tolist()))

    arr = np.array([r[0] for r in rec])
    collision_list = sorted(collisions.values(), key=lambda event: event["start_s"])
    res = SwingResult(
        t=arr,
        pos=np.array([r[1] for r in rec]),
        quat=np.array([r[2] for r in rec]),
        theta_deg=np.array([r[3] for r in rec]),
        omega=np.array([r[4] for r in rec]),
        wind_ms=np.array([r[5] for r in rec]),
        wind_force_N=np.array([r[6] for r in rec]),
        tension_N=np.array([r[7] for r in rec]),
        tagline_force_N=np.array([r[8] for r in rec]),
        anchor=(ax, ay, az),
        meta={"engine": "mujoco", "mass_kg": mass_kg, "length_m": length_m, "area_m2": area_m2,
              "cd": cd, "rho": rho, "timestep": timestep, "duration_s": duration_s,
              "tagline_capacity_N": tagline_capacity_N, "initial_theta_deg": initial_theta_deg,
              "wind_dir_deg": math.degrees(math.atan2(wdir[1], wdir[0])),
              "moving_anchor": anchor_fn is not None, "collision_count": len(collision_list),
              "collision_boxes": len(collision_boxes),
              "contact_max_normal_force_N": max((event["max_normal_force_N"] for event in collision_list), default=0.0),
              "contact_max_impact_speed_m_s": max((event["max_impact_speed_m_s"] for event in collision_list), default=0.0),
              "contact_force_converged": False},
        anchor_track=np.array([r[9] for r in rec]) if anchor_fn else None,
        collisions=collision_list,
    )
    return res


# -- аналитическая (быстрая) модель --------------------------------------
def _simulate_analytic(wind_speed_fn, duration_s, mass_kg, length_m, area_m2, cd, rho,
                       anchor, wdir, tagline_capacity_N, timestep, sample_every_s,
                       initial_theta_deg, g) -> SwingResult:
    """Нелинейный маятник с аэродинамическим демпфированием и оттяжкой.

    Уравнение движения в плоскости ветра (theta — ЗНАКОВЫЙ угол от вертикали):

        m*L*th'' = -m*g*sin(th) + F_x*cos(th) + F_z*sin(th) + F_tag*cos(th)

    F_x, F_z — проекции аэродинамической силы, посчитанной ПОЛНОСТЬЮ в 3D по
    относительной скорости воздуха и груза (включая вертикальную составляющую
    скорости груза). Это принципиально: если в аналитике учесть только
    горизонтальную относительную скорость, а в MuJoCo — полную, движки
    разойдутся на ~10 % и сверка потеряет смысл. Здесь аэродинамика в обоих
    движках одна и та же, поэтому остаточное расхождение определяется только
    собственным моментом инерции панели (в аналитике груз — материальная
    точка, в MuJoCo — параллелепипед с честной инерцией).

    Используется для Monte Carlo: в десятки раз быстрее MuJoCo при той же физике.
    """
    ax, ay, az = anchor
    L = float(length_m)
    m = float(mass_kg)
    dt = float(max(timestep, 1e-4))
    th = math.radians(float(initial_theta_deg))
    om = 0.0
    sample_dt = max(dt, float(sample_every_s))
    sample_every = max(1, int(round(sample_dt / dt)))
    rec = []
    n_steps = int(round(duration_s / dt))
    q_aero = 0.5 * rho * cd * area_m2          # скоростной напор * Cd * A / 2

    for k in range(n_steps):
        t = k * dt
        v_w = float(wind_speed_fn(t))
        # скорость груза: x = L*sin(th), z = az - L*cos(th)
        vx = L * om * math.cos(th)
        vz = L * om * math.sin(th)
        rx, rz = v_w - vx, -vz                 # относительная скорость воздуха
        speed = math.hypot(rx, rz)
        F_x = q_aero * speed * rx
        F_z = q_aero * speed * rz

        F_tag = 0.0
        if tagline_capacity_N > 0:
            off = L * math.sin(th)
            F_tag = -math.copysign(min(tagline_capacity_N, 60.0 * abs(off)), off)

        acc = (-m * g * math.sin(th) + (F_x + F_tag) * math.cos(th)
               + F_z * math.sin(th)) / (m * L)
        om += acc * dt
        th += om * dt

        if k % sample_every == 0:
            sx = L * math.sin(th)
            x = ax + sx * wdir[0]
            y = ay + sx * wdir[1]
            z = az - L * math.cos(th)
            quat = [math.cos(th / 2.0), 0.0, math.sin(th / 2.0), 0.0]
            F_stat = q_aero * v_w ** 2
            tension = suspension_tension_N(m, math.degrees(th), om, L, wind_force_N=F_stat, g=g)
            rec.append((t, [x, y, z], quat, math.degrees(th), om, v_w, F_stat, tension, abs(F_tag)))

    res = SwingResult(
        t=np.array([r[0] for r in rec]),
        pos=np.array([r[1] for r in rec]),
        quat=np.array([r[2] for r in rec]),
        theta_deg=np.array([r[3] for r in rec]),
        omega=np.array([r[4] for r in rec]),
        wind_ms=np.array([r[5] for r in rec]),
        wind_force_N=np.array([r[6] for r in rec]),
        tension_N=np.array([r[7] for r in rec]),
        tagline_force_N=np.array([r[8] for r in rec]),
        anchor=(ax, ay, az),
        meta={"engine": "analytic", "mass_kg": m, "length_m": L, "area_m2": area_m2, "cd": cd,
              "rho": rho, "timestep": dt, "duration_s": duration_s,
              "tagline_capacity_N": tagline_capacity_N, "initial_theta_deg": initial_theta_deg},
    )
    return res
