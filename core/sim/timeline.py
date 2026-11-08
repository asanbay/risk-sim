"""
core/sim/timeline.py — траектории и единый таймлайн сцены.

Это тот самый слой [3] из архитектуры: ядро считает ПОЛОЖЕНИЯ ПО ВРЕМЕНИ
и отдаёт их в одном JSON. Blender, Three.js и Streamlit читают один и тот же
файл — поэтому «витрину» можно менять, не трогая расчёты.

Что описано:
  LoadTrajectory  — движение груза в течение одного подъёма (фазы ППР:
                    строповка -> подъём -> поворот/тележка -> наведение ->
                    опускание -> отцепка) + раскачка от ветра.
  CraneState      — состояние крана, ВЫВЕДЕННОЕ из положения груза
                    (вылет, угол поворота, высота крюка). Кран не «анимируется
                    отдельно»: он следует за грузом, как в реальности.
  build_timeline  — сборка кадров для экспорта.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

from core.context import Context
from core.io.loaders import CraneSpec, Lift, WorkTask
from core.physics.pendulum import static_deflection_deg


# ---------------------------------------------------------------------------
# фазы подъёма (доли длительности окна задачи) — настраиваются из ППР
# ---------------------------------------------------------------------------
DEFAULT_PHASES: dict[str, tuple[float, float]] = {
    "rig":     (0.00, 0.10),   # строповка на складе
    "hoist":   (0.10, 0.30),   # подъём до монтажной высоты
    "travel":  (0.30, 0.62),   # поворот стрелы + тележка
    "position":(0.62, 0.78),   # наведение на место приёмки (оттяжки)
    "lower":   (0.78, 0.92),   # опускание
    "unhook":  (0.92, 1.00),   # отцепка, подъём крюка
}


@dataclass
class LoadTrajectory:
    lift: Lift
    task: WorkTask
    phases: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_PHASES))
    swing_offset_m: float = 0.0        # установившееся смещение от ветра (из физики)
    swing_theta_deg: float = 0.0
    wind_dir_deg: float = 200.0

    # ------------------------------------------------------------------
    @property
    def t0(self) -> pd.Timestamp:
        return self.task.start

    @property
    def dur_s(self) -> float:
        return (self.task.end - self.task.start).total_seconds()

    def phase_at(self, frac: float) -> str:
        for name, (a, b) in self.phases.items():
            if a <= frac <= b:
                return name
        return "unhook"

    def at(self, t: pd.Timestamp) -> dict[str, Any]:
        """Положение груза и состояние крана в момент t."""
        dt = (pd.Timestamp(t) - self.t0).total_seconds()
        if dt < 0:
            frac = 0.0
        elif dt > self.dur_s:
            frac = 1.0
        else:
            frac = dt / self.dur_s if self.dur_s > 0 else 1.0
        return self.at_frac(frac)

    def at_frac(self, frac: float) -> dict[str, Any]:
        px, py, pz = self.lift.pickup
        dx, dy, dz = self.lift.dropoff
        hook_z = float(self.lift.hook_height_m)
        ph = self.phase_at(frac)

        def sub(a: float, b: float) -> float:
            lo, hi = self.phases[ph]
            return 0.0 if hi <= lo else float(np.clip((frac - lo) / (hi - lo), 0.0, 1.0))

        s = sub(*self.phases[ph])
        # вертикаль и горизонталь по фазам
        if ph == "rig":
            x, y, z = px, py, pz
        elif ph == "hoist":
            x, y, z = px, py, pz + (hook_z - self.lift.suspend_length_m - pz) * s
        elif ph == "travel":
            x, y, z = px + (dx - px) * s, py + (dy - py) * s, hook_z - self.lift.suspend_length_m
        elif ph == "position":
            x, y, z = dx, dy, hook_z - self.lift.suspend_length_m
        elif ph == "lower":
            x, y, z = dx, dy, (hook_z - self.lift.suspend_length_m) + (dz - (hook_z - self.lift.suspend_length_m)) * s
        else:  # unhook
            x, y, z = dx, dy, dz

        # раскачка: смещение по направлению ветра, максимально в фазах travel/position
        gain = {"rig": 0.0, "hoist": 0.6, "travel": 1.0, "position": 0.8, "lower": 0.4, "unhook": 0.0}[ph]
        wdx = math.cos(math.radians(self.wind_dir_deg))
        wdy = math.sin(math.radians(self.wind_dir_deg))
        off = self.swing_offset_m * gain
        x += off * wdx
        y += off * wdy

        return {"frac": round(float(frac), 5), "phase": ph,
                "x": float(x), "y": float(y), "z": float(z),
                "swing_offset_m": float(off), "swing_theta_deg": float(self.swing_theta_deg * gain)}

    def sample(self, dt_s: float = 60.0) -> list[dict[str, Any]]:
        n = max(2, int(self.dur_s // dt_s))
        return [self.at_frac(i / (n - 1)) for i in range(n)]


def crane_state(crane: CraneSpec, load_x: float, load_y: float, load_z: float,
                suspend_length_m: float) -> dict[str, Any]:
    """Состояние крана, выведенное из положения груза (а не наоборот)."""
    dx, dy = load_x - crane.mast_x, load_y - crane.mast_y
    radius = math.hypot(dx, dy)
    slew = math.degrees(math.atan2(dy, dx)) % 360.0
    return {
        "slew_deg": round(slew, 3),
        "radius_m": round(float(np.clip(radius, crane.trolley_min_r, crane.trolley_max_r)), 3),
        "radius_raw_m": round(radius, 3),
        "hook_z_m": round(load_z + suspend_length_m, 3),
        "jib_head_z_m": crane.head_z,
        "mast_x": crane.mast_x, "mast_y": crane.mast_y,
        "capacity_kg": round(crane.capacity_kg(radius), 1),
    }


# ---------------------------------------------------------------------------
# сборка таймлайна
# ---------------------------------------------------------------------------
def build_timeline(ctx: Context, *, crew_tracks: Sequence[dict[str, Any]] | None = None,
                   hazard_windows: Sequence[dict[str, Any]] | None = None,
                   dt_min: float = 5.0,
                   start: pd.Timestamp | None = None,
                   end: pd.Timestamp | None = None) -> dict[str, Any]:
    """Кадры сцены для Blender/Three.js: груз, кран, бригады, зоны, ветер.

    Формат специально плоский и версионируемый (schema_version), чтобы через
    год можно было добавить Unreal/Omniverse как ещё одного потребителя.
    """
    ds, cfg, wind = ctx.ds, ctx.cfg, ctx.wind
    t0 = pd.Timestamp(start) if start is not None else min(t.start for t in ds.tasks)
    t1 = pd.Timestamp(end) if end is not None else max(t.end for t in ds.tasks)
    frames_ts = pd.date_range(t0, t1, freq=f"{int(round(dt_min))}min")

    # траектории всех подъёмов
    lifts: dict[str, LoadTrajectory] = {}
    for lf in ds.lifts:
        task = ds.task_by_id[lf.task_id]
        la = next((a for a in ctx.lift_assessments if a.lift.lift_id == lf.lift_id), None)
        swing_off = float(la.swing.get("steady_offset_m", 0.0)) if la else 0.0
        swing_th = float(la.swing.get("max_theta_deg", 0.0)) if la else 0.0
        lifts[lf.lift_id] = LoadTrajectory(lf, task, swing_offset_m=swing_off,
                                           swing_theta_deg=swing_th)

    frames: list[dict[str, Any]] = []
    for ts in frames_ts:
        active_lifts = []
        crane = None
        for lf in ds.lifts:
            task = ds.task_by_id[lf.task_id]
            if not (task.start <= ts <= task.end):
                continue
            st = lifts[lf.lift_id].at(ts)
            cs = crane_state(ds.crane, st["x"], st["y"], st["z"], lf.suspend_length_m)
            active_lifts.append({"lift_id": lf.lift_id, "task_id": lf.task_id,
                                 "load_name": lf.load_name, "mass_kg": lf.mass_kg, **st})
            crane = cs  # в демо один кран; при нескольких — список
        v_ref = wind.gust_at(ts, wind.ref_height_m)
        v_top = wind.gust_at(ts, ctx.max_work_height_m())
        frames.append({
            "t": ts.strftime("%Y-%m-%d %H:%M"),
            "frame_index": len(frames),
            "wind": {"gust_at_ref_ms": round(v_ref, 2),
                     "gust_at_top_ms": round(v_top, 2),
                     "mean_at_ref_ms": round(wind.mean_at(ts, wind.ref_height_m), 2)},
            "crane": crane,
            "loads": active_lifts,
            "tasks_active": [t.task_id for t in ds.tasks if t.start <= ts <= t.end],
        })

    return {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meta": {
            "project": cfg.meta.get("project"),
            "rules_version": cfg.meta.get("version"),
            "disclaimer": cfg.disclaimer,
            "dt_min": dt_min,
            "t_start": t0.strftime("%Y-%m-%d %H:%M"),
            "t_end": t1.strftime("%Y-%m-%d %H:%M"),
            "units": {"length": "m", "angle": "deg", "mass": "kg", "force": "N", "speed": "m/s"},
            "up_axis": "Z",
        },
        "crane": ds.crane.to_dict(),
        "zones": [z.to_dict() for z in ds.zones],
        "crews": [{"crew_id": c.crew_id, "name": c.name, "size": c.size, "color": c.color}
                  for c in ds.crews],
        "tasks": [t.to_dict() for t in ds.tasks],
        "lifts": [l.to_dict() for l in ds.lifts],
        "crew_tracks": list(crew_tracks or []),
        "hazard_windows": list(hazard_windows or []),
        "frames": frames,
    }
