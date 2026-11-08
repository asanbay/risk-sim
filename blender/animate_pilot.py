"""
blender/animate_pilot.py — анимация сцены ПО results/pilot/scene_bundle.json.

Запуск (сначала должен быть построен results/pilot/building.blend):
    blender -b results/pilot/building.blend \
        -P blender/animate_pilot.py -- --bundle results/pilot/scene_bundle.json [--every 4]

Что анимируется (иерархия из scene_setup.py):
  Crane_Slew    — поворот стрелы (rotation_euler.z)
  Crane_Trolley — вылет (location.x вдоль стрелы)
  Crane_Hook    — высота крюка (location.z вниз от тележки)
  Crane_Rope    — длина троса (scale.z)
  Load          — МИРОВАЯ позиция груза + наклон от раскачки (swing_offset_m)
  Crew_*        — позиции бригад из crew_tracks (интерполяция по времени)
  Zone_*        — подсветка риска: obj.color -> красный, пока зона входит в
                  активное нарушение (материал читает Object Info -> Color,
                  поэтому ноды трогать не нужно)

Кадр Blender i = frames[i * every]. По умолчанию fps=24, так что неделя
работ превращается в ~30 с ролика инструктажа. Реальное время не нужно:
ролик объясняет ПОСЛЕДОВАТЕЛЬНОСТЬ событий, а длительности берутся из отчёта.

Никаких чисел «от себя»: всё, что движется, приходит из JSON ядра.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import bpy

HIGHLIGHT = (1.0, 0.12, 0.12, 1.0)


def log(*a):
    print("[animate]", *a, flush=True)


def get(name):
    return bpy.data.objects.get(name)


def kf(ob, path, frame, index=None):
    if ob is None:
        return
    if index is None:
        ob.keyframe_insert(data_path=path, frame=frame)
    else:
        ob.keyframe_insert(data_path=path, index=index, frame=frame)


def ts(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%d %H:%M")


def zone_type_lookup(zones) -> dict:
    out = {}
    for z in zones:
        out[z.get("id") or z.get("zone_id")] = z.get("type", "work")
    return out


def work_focus(zones):
    level = max(float(z["z_floor"]) for z in zones)
    active = [z for z in zones if float(z["z_floor"]) >= level - 1 and z.get("type") != "crane_hazard"]
    x_min = min(float(z["x_min"]) for z in active)
    x_max = max(float(z["x_max"]) for z in active)
    y_min = min(float(z["y_min"]) for z in active)
    y_max = max(float(z["y_max"]) for z in active)
    return (x_min + x_max) / 2, (y_min + y_max) / 2, level + 2, max(x_max - x_min, y_max - y_min)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default="results/pilot/scene_bundle.json")
    ap.add_argument("--every", type=int, default=1, help="брать каждый N-й кадр таймлайна")
    ap.add_argument("--fps", type=int, default=0, help="переопределить fps сцены")
    ap.add_argument("--no-crew", action="store_true")
    a = ap.parse_args(argv)

    bundle = json.loads(Path(a.bundle).read_text(encoding="utf-8"))
    tl = bundle["timeline"]
    frames = tl["frames"][::max(1, a.every)]
    if not frames:
        log("нет кадров — нечего анимировать")
        return
    crane_meta = tl["crane"]
    mx, my = crane_meta["mast"]["x"], crane_meta["mast"]["y"]
    top = crane_meta["mast"]["top_z"]
    hook_dz = -1.6                      # крюк ниже тележки (из scene_setup)
    trolley_r_min = max(4.0, crane_meta["jib"].get("min_radius_m", 4.0))
    trolley_r_max = crane_meta["jib"]["length_m"] - 1.0

    scn = bpy.context.scene
    if a.fps:
        scn.render.fps = a.fps
    scn.frame_start = 0
    scn.frame_end = len(frames) - 1

    slew, trolley, hook, rope = get("Crane_Slew"), get("Crane_Trolley"), get("Crane_Hook"), get("Crane_Rope")
    load = get("Load")

    # --- бригады: точки с меткой времени, чтобы интерполировать ---
    crew_objs: dict[str, list] = {}
    for ob in bpy.data.objects:
        if ob.name.startswith("Crew_"):
            crew_objs.setdefault(ob.name.split("_")[1], []).append(ob)
    tracks = {}
    for c in (tl.get("crew_tracks") or []):
        pts = c.get("points") or []
        if pts:
            tracks[c["crew_id"]] = [(ts(p["t"]), p["x"], p["y"], p["z"], p.get("state", "")) for p in pts]
    log(f"треков бригад: {len(tracks)}")

    ztypes = zone_type_lookup(tl["zones"])
    zone_objs = {ob.name[len("Zone_"):]: ob for ob in bpy.data.objects if ob.name.startswith("Zone_")}

    # --- окна нарушений: (start, end, {zone_ids}) ---
    wins = []
    for f in bundle.get("findings", []):
        zids = f.get("zone_ids") or []
        if isinstance(zids, str):
            zids = [z for z in zids.split(";") if z]
        try:
            wins.append((ts(f["start"]), ts(f["end"]), set(zids), f.get("severity", "")))
        except (KeyError, ValueError):
            continue
    log(f"окон нарушений: {len(wins)}")

    def crew_at(cid, t):
        pts = tracks.get(cid)
        if not pts:
            return None
        if t <= pts[0][0]:
            return pts[0]
        if t >= pts[-1][0]:
            return pts[-1]
        lo, hi = 0, len(pts) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if pts[mid][0] <= t:
                lo = mid
            else:
                hi = mid
        t0, x0, y0, z0, s0 = pts[lo]
        t1, x1, y1, z1, s1 = pts[hi]
        dt = max(1e-6, (t1 - t0).total_seconds())
        k = (t - t0).total_seconds() / dt
        return (t0, x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, z0 + (z1 - z0) * k, s0)

    parked_slew = math.radians(20.0)
    for i, f in enumerate(frames):
        fr = i
        t = ts(f["t"])
        cr = f.get("crane") or {}
        loads = f.get("loads") or []

        # --- кран ---
        if cr and cr.get("slew_deg") is not None:
            slew.rotation_euler[2] = math.radians(float(cr["slew_deg"]))
            r = min(max(float(cr.get("radius_m", 20.0)), trolley_r_min), trolley_r_max)
            trolley.location.x = r
            hz = float(cr.get("hook_z_m", top - 20))
            hook.location.z = hook_dz + (hz - (top + 4))
            kf(slew, "rotation_euler", fr, index=2)
            kf(trolley, "location", fr, index=0)
            kf(hook, "location", fr, index=2)
            if loads:
                susp = max(1.0, (hz - float(loads[0]["z"]) - 2.0))
                rope.scale.z = susp
                kf(rope, "scale", fr, index=2)
        else:
            slew.rotation_euler[2] = parked_slew
            trolley.location.x = trolley_r_min + 2
            hook.location.z = hook_dz - 6
            kf(slew, "rotation_euler", fr, index=2)
            kf(trolley, "location", fr, index=0)
            kf(hook, "location", fr, index=2)
            rope.scale.z = 6.0
            kf(rope, "scale", fr, index=2)

        # --- груз ---
        if load is not None:
            if loads:
                l0 = loads[0]
                lx, ly, lz = float(l0["x"]), float(l0["y"]), float(l0["z"])
                off = float(l0.get("swing_offset_m", 0.0) or 0.0)
                load.location = (lx, ly, lz)
                # наклон: раскачка отклоняет груз в плоскости стрелы
                ang = math.atan2(off, max(1.0, (top - lz))) if off > 0.02 else 0.0
                slew_a = math.radians(float(cr.get("slew_deg", 0.0))) if cr else 0.0
                load.rotation_euler = (math.sin(slew_a) * ang, -math.cos(slew_a) * ang, slew_a)
                kf(load, "location", fr)
                kf(load, "rotation_euler", fr)
                load.hide_render = load.hide_viewport = False
            else:
                load.hide_render = load.hide_viewport = True
            kf(load, "hide_render", fr)
            kf(load, "hide_viewport", fr)

        # --- бригады ---
        if not a.no_crew:
            for cid, objs in crew_objs.items():
                p = crew_at(cid, t)
                if not p:
                    continue
                _, px, py, pz, state = p
                for j, ob in enumerate(objs):
                    dx = ((j % 3) - 1) * 1.15
                    dy = ((j // 3) % 3 - 1) * 1.15
                    ob.location = (px + dx, py + dy, pz + 0.9)
                    kf(ob, "location", fr)
                    # «работает» — стоит, «идёт» — чуть приподнят (условно, для читаемости)
                    ob.scale.z = 1.0 if state == "working" else 0.92
                    kf(ob, "scale", fr, index=2)

        # --- подсветка зон риска ---
        act = set()
        for s, e, zs, sev in wins:
            if s <= t <= e:
                act |= zs
        for z, ob in zone_objs.items():
            base = ob.get("base_color")
            if z in act:
                ob.color = HIGHLIGHT
            elif base:
                ob.color = (base[0], base[1], base[2], 1.0)
            kf(ob, "color", fr)

    # камера: лёгкий облёт вокруг ядра, чтобы ролик не был статичным
    cam = get("Camera")
    tgt = get("CamTarget")
    if cam is not None and tgt is not None:
        fx, fy, fz, span = work_focus(tl["zones"])
        focus_lift = max(0.0, (top - fz) * 0.35)
        target_z = fz + focus_lift
        radius = max(70.0, span * 1.4) * (1.25 if focus_lift > 10 else 1.0)
        n = len(frames) - 1
        for fr, ang in ((0, 35.0), (n // 3, -20.0), (2 * n // 3, 60.0), (n, 35.0)):
            rad = math.radians(ang)
            cam.location = (fx + radius * math.cos(rad), fy - radius * math.sin(rad), target_z + 45 - 15 * math.sin(rad * 2))
            kf(cam, "location", fr)
        tgt.location = (fx, fy, target_z)
        kf(tgt, "location", 0)

    bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
    log(f"кадров анимации: {len(frames)} (0..{len(frames)-1}), шаг таймлайна ×{a.every}")
    log("готово. Рендер: blender/render.sh  (или F12 в GUI)")


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    main(argv)
