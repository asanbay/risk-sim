"""
blender/animate_realistic.py — покадровая анимация реалистичной сцены ПО JSON.

Запуск (после scene_realistic.py):
    blender -b results/dynamics/crane_scene.blend \
        -P blender/animate_realistic.py -- --bundle ... [--every 4]

Физика и числа только из JSON ядра:
  * Crane_Slew / Crane_Trolley / Crane_Hook / Crane_Rope — поворот, вылет,
    высота крюка и длина троса из frames[i].crane;
  * Load — мировая позиция груза из frames[i].loads; наклон = угол маятника
    tan(theta) = offset / L (theta или swing_offset_m из расчёта раскачки);
  * Sun и небо Nishita — астрономия NOAA по метке времени кадра:
    высота и азимут солнца непрерывно едут по неделе работ;
  * Crew_* — интерполяция по crew_tracks + лёгкий шаг при ходьбе;
  * Zone_* — подсветка риска в окна нарушений.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import bpy
from mathutils import Vector

HIGHLIGHT = (1.0, 0.10, 0.10, 1.0)


def log(*a):
    print("[animate_realistic]", *a, flush=True)


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


def sun_direction(dt, lat_deg, lon_deg, tz_hours):
    lat = math.radians(lat_deg)
    day = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    g = 2 * math.pi / 365.0 * (day - 1 + (hour - 12) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    time_offset = eqtime + 4 * lon_deg - 60 * tz_hours
    tst = hour * 60 + time_offset
    ha = math.radians(tst / 4.0 - 180.0)
    cos_zen = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    elevation = math.asin(max(-1.0, min(1.0, cos_zen)))
    cos_az = (math.sin(decl) - math.sin(elevation) * math.sin(lat)) / \
             max(1e-6, math.cos(elevation) * math.cos(lat))
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.acos(cos_az)
    if ha > 0:
        az = 2 * math.pi - az
    return elevation, az


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default="results/dynamics/scene_bundle.json")
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--fps", type=int, default=0)
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
    head_z = crane_meta["jib"].get("head_z", top)
    r_min = max(4.0, crane_meta["jib"].get("min_radius_m", 4.0))
    r_max = crane_meta["jib"]["length_m"]   # паспортный вылет без урезания

    scn = bpy.context.scene
    trolley_z = float(scn.get("trolley_z", 0.25))
    ring_h = float(scn.get("ring_h", 1.8))
    scn["risk_every"] = max(1, a.every)
    if a.fps:
        scn.render.fps = a.fps
    scn.frame_start = 0
    scn.frame_end = len(frames) - 1

    slew, trolley, hook, rope = (get(n) for n in ("Crane_Slew", "Crane_Trolley", "Crane_Hook", "Crane_Rope"))
    load, ring = get("Load"), get("Load_Ring")
    sun = get(scn.get("sun_object", "Sun"))
    if sun is not None:
        sun.rotation_mode = "QUATERNION"

    def classify_load(name: str) -> str:
        n = str(name).lower()
        if "арматур" in n:
            return "rebar"
        if "бетон" in n or "бадья" in n:
            return "bucket"
        if "секц" in n:
            return "section"
        return "panel"

    kind_objs = {k: get(f"Load_{k}") for k in str(scn.get("load_kinds", "panel")).split(",") if k}
    windsock = get("Windsock")
    gauge10 = get("Gauge_Wind10_Bar")
    gauge_top = get("Gauge_WindTop_Bar")
    lift_frames: list[int] = []
    sky_node_name = scn.get("sky_node")
    sky = wnt.nodes.get(sky_node_name) if (wnt := scn.world.node_tree if scn.world else None) else None
    lat = float(scn.get("lat", 41.0))
    lon = float(scn.get("lon", 29.0))
    tz = float(scn.get("tz", 3.0))
    log(f"солнце: {sun.name if sun else '—'}, небо: {sky.name if sky else '—'}, lat={lat}")

    crew_objs: dict[str, list] = {}
    for ob in bpy.data.objects:
        if ob.name.startswith("Crew_"):
            crew_objs.setdefault(ob.name.split("_")[1], []).append(ob)
    tracks = {}
    for c in (tl.get("crew_tracks") or []):
        pts = c.get("points") or []
        if pts:
            tracks[c["crew_id"]] = [(ts(p["t"]), p["x"], p["y"], p["z"], p.get("state", "")) for p in pts]

    ztypes = {}
    for z in tl["zones"]:
        ztypes[z.get("id") or z.get("zone_id")] = z.get("type", "work")
    zone_objs = {ob.name[len("Zone_"):]: ob for ob in bpy.data.objects if ob.name.startswith("Zone_")}

    wins = []
    for f in bundle.get("findings", []):
        zids = f.get("zone_ids") or []
        if isinstance(zids, str):
            zids = [z for z in zids.split(";") if z]
        try:
            wins.append((ts(f["start"]), ts(f["end"]), set(zids)))
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
        return (t0, x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, z0 + (z1 - z0) * k, s1 or s0)

    parked = math.radians(25.0)
    elev_min, elev_max = 90.0, -90.0
    for i, f in enumerate(frames):
        fr = i
        t = ts(f["t"])
        cr = f.get("crane") or {}
        loads = f.get("loads") or []

        # --- кран: кинематика из расчёта ---
        if cr and cr.get("slew_deg") is not None:
            slew.rotation_euler[2] = math.radians(float(cr["slew_deg"]))
            r = min(max(float(cr.get("radius_m", 20.0)), r_min), r_max)
            trolley.location.x = r
            hz = float(cr.get("hook_z_m", top - 20))
            # крюк-пустышка ровно на hook_z_m: локальная высота от тележки
            hook.location.z = hz - (top + trolley_z)
            kf(slew, "rotation_euler", fr, index=2)
            kf(trolley, "location", fr, index=0)
            kf(hook, "location", fr, index=2)
        else:
            slew.rotation_euler[2] = parked
            trolley.location.x = r_min + 2
            hook.location.z = -4.0
            kf(slew, "rotation_euler", fr, index=2)
            kf(trolley, "location", fr, index=0)
            kf(hook, "location", fr, index=2)

        # --- груз и маятник ---
        if load is not None:
            if loads:
                l0 = loads[0]
                lift_frames.append(fr)
                lx, ly, lz = float(l0["x"]), float(l0["y"]), float(l0["z"])
                load.location = (lx, ly, lz)
                off = float(l0.get("swing_offset_m", 0.0) or 0.0)
                theta = float(l0.get("swing_theta_deg", 0.0) or 0.0)
                if abs(theta) > 0.05:
                    ang = math.radians(theta)
                elif off > 0.02:
                    hang = max(2.0, hz - lz - 3.0)
                    ang = math.atan2(off, hang)
                else:
                    ang = 0.0
                slew_a = math.radians(float(cr.get("slew_deg", 0.0))) if cr else 0.0
                # качание в плоскости стрелы (радиальное направление)
                load.rotation_euler = (math.sin(slew_a) * ang, -math.cos(slew_a) * ang, slew_a)
                kf(load, "location", fr)
                kf(load, "rotation_euler", fr)
                load.hide_render = load.hide_viewport = False
                # включаем мешь того типа, что реально поднят
                active_kind = classify_load(l0.get("load_name", ""))
                for kind, kind_ob in kind_objs.items():
                    kind_ob.hide_render = kind_ob.hide_viewport = (kind != active_kind)
                    kf(kind_ob, "hide_render", fr)
                    kf(kind_ob, "hide_viewport", fr)
            else:
                load.hide_render = load.hide_viewport = True
                for kind_ob in kind_objs.values():
                    kind_ob.hide_render = kind_ob.hide_viewport = True
                    kf(kind_ob, "hide_render", fr)
                    kf(kind_ob, "hide_viewport", fr)
            kf(load, "hide_render", fr)
            kf(load, "hide_viewport", fr)

        # --- трос: длина от крюка до кольца строп (верх троса — в крюке) ---
        if rope is not None:
            if loads:
                l0 = loads[0]
                hz = float(cr.get("hook_z_m", top - 20)) if cr else top - 20
                ring_z = float(l0["z"]) + ring_h
                rope.scale.z = max(0.35, hz - ring_z)
            else:
                rope.scale.z = 3.0
            kf(rope, "scale", fr, index=2)

        # --- приборы площадки: ветроконус и шкалы ветра ---
        wind = f.get("wind") or {}
        v_ref = float(wind.get("gust_at_ref_ms", 0.0) or 0.0)
        v_top = float(wind.get("gust_at_top_ms", 0.0) or 0.0)
        if windsock is not None:
            # калибровка ветроконуса: 1.5 м/с — висит (78°), 9 м/с — горизонт
            k = max(0.0, min(1.0, (v_ref - 1.5) / (9.0 - 1.5)))
            windsock.rotation_euler[1] = -math.radians((1.0 - k) * 78.0)
            kf(windsock, "rotation_euler", fr, index=1)
        for bar, value in ((gauge10, v_ref), (gauge_top, v_top)):
            if bar is not None:
                s = max(0.02, min(value, 24.0))
                bar.scale.z = s
                bar.location.z = 0.5 * s
                kf(bar, "scale", fr, index=2)
                kf(bar, "location", fr, index=2)

        # --- солнце и небо: астрономия по времени кадра ---
        el, az = sun_direction(t, lat, lon, tz)
        elev_min, elev_max = min(elev_min, math.degrees(el)), max(elev_max, math.degrees(el))
        if sun is not None:
            if el > math.radians(-1.0):
                toward = Vector((math.cos(el) * math.sin(az), math.cos(el) * math.cos(az), math.sin(el)))
                sun.rotation_quaternion = (-toward).to_track_quat("-Z", "Y")
                sun.hide_render = False
                sun.data.energy = 35.0
            else:
                sun.hide_render = True
                sun.data.energy = 0.0
            kf(sun, "rotation_quaternion", fr)
            kf(sun, "hide_render", fr)
            kf(sun.data, "energy", fr)
        if sky is not None:
            for inp in sky.inputs:
                if inp.name.lower().startswith("sun_elevation"):
                    inp.default_value = max(-0.02, el)
                    inp.keyframe_insert(data_path="default_value", frame=fr)
                elif inp.name.lower().startswith("sun_rotation"):
                    inp.default_value = az - math.pi
                    inp.keyframe_insert(data_path="default_value", frame=fr)

        # --- бригады ---
        if not a.no_crew:
            for cid, objs in crew_objs.items():
                p = crew_at(cid, t)
                if not p:
                    continue
                _, px, py, pz, state = p
                walking = state not in ("working", "work", "")
                for j, ob in enumerate(objs):
                    dx = ((j % 3) - 1) * 1.15
                    dy = ((j // 3) % 3 - 1) * 1.15
                    bob = abs(math.sin((fr * 0.9 + j) % (2 * math.pi))) * 0.06 if walking else 0.0
                    ob.location = (px + dx, py + dy, pz + 0.0)
                    kf(ob, "location", fr)
                    ob.scale.z = 1.0
                    ob.location.z += bob
                    kf(ob, "location", fr, index=2)

        # --- подсветка зон риска ---
        act = set()
        for s, e, zs in wins:
            if s <= t <= e:
                act |= zs
        for z, ob in zone_objs.items():
            base = ob.get("base_color")
            if z in act:
                ob.color = HIGHLIGHT
            elif base:
                ob.color = (base[0], base[1], base[2], 1.0)
            kf(ob, "color", fr)

    log(f"высота солнца за период: {elev_min:.1f}° .. {elev_max:.1f}°")
    scn["lift_frames"] = lift_frames
    log(f"кадров с подъёмами (для панели): {len(lift_frames)}")

    # --- комфортное открытие файла: самый светлый кадр, камера-облёт ---
    over = get("Cam_Overview")
    if over is not None:
        scn.camera = over
    best_frame, best_el = 0, -90.0
    for i, f in enumerate(frames):
        el_i, _ = sun_direction(ts(f["t"]), lat, lon, tz)
        if math.degrees(el_i) > best_el:
            best_el, best_frame = math.degrees(el_i), i
    scn.frame_set(best_frame)
    scn["open_frame"] = best_frame
    log(f"старт файла: кадр {best_frame} (солнце {best_el:.0f}°), камера {scn.camera.name if scn.camera else '—'}")

    # --- камера-облёт: вся башня от земли до оголовка ---
    cam = get("Cam_Overview")
    tgt = get("CamTarget")
    if cam is not None and tgt is not None:
        crane_meta = tl["crane"]
        tower_top = max(crane_meta["jib"].get("head_z", crane_meta["mast"]["top_z"]),
                        crane_meta["mast"]["top_z"]) + 10
        top_level = max(float(z["z_floor"]) for z in tl["zones"])
        active = [z for z in tl["zones"] if float(z["z_floor"]) >= top_level - 1 and z.get("type") != "crane_hazard"]
        fx = (min(z["x_min"] for z in active) + max(z["x_max"] for z in active)) / 2
        fy = (min(z["y_min"] for z in active) + max(z["y_max"] for z in active)) / 2
        span = max(max(z["x_max"] for z in active) - min(z["x_min"] for z in active),
                   max(z["y_max"] for z in active) - min(z["y_min"] for z in active))
        radius = max(320.0, tower_top * 1.8)
        n = max(1, len(frames) - 1)
        for fr, ang, h in ((0, 35.0, 0.13), (n // 3, -25.0, 0.09), (2 * n // 3, 65.0, 0.18), (n, 35.0, 0.13)):
            rad = math.radians(ang)
            cam.location = (fx + radius * math.cos(rad), fy - radius * math.sin(rad),
                            tower_top * h)
            cam.keyframe_insert(data_path="location", frame=fr)
        tgt.location = (fx, fy, tower_top * 0.44)
        kf(tgt, "location", 0)

    bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
    log(f"кадров: {len(frames)} (0..{len(frames)-1})")


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    main(argv)
