"""
risk_sim/usd_stage.py — scene_bundle.json → OpenUSD stage (единый формат платформы).

Этап 1 плана «уровня Omniverse»: сцена недели работ живёт в OpenUSD.
Структура stage:

    /RiskSim                     (assembly, customLayerData: проект)
      ├── Site                   земля и твёрдое основание
      ├── Building               меш model.glb (импорт IFC) или процедурный
      ├── Crane                  башня по данным расчёта
      │     └── Slew             rotateZ = slew_deg (timeSamples)
      │           ├── Jib / Trolley / Hook / Rope
      ├── Loads                  4 типа грузов, visibility по кадрам
      ├── Zones/<zone_id>        полупрозрачные объёмы рисков
      ├── Clearance/<idx>        маркеры клиренса (цвет = статус)
      ├── Crews/C<id>_<n>        капсулы по tracks агентов
      ├── Lights                 DomeLight + SunLight (NOAA, timeSamples)
      └── Cameras                Overview / Crane / Ground

Валидация: validate_stage() открывает stage заново и сверяет timeSamples
с numbers из bundle (slew, translate груза) — гейты 0.5° / 5 см, как у
Blender-валидатора. Итог — scene_stage_validation.json.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

try:
    from pxr import Gf, Kind, Sdf, Usd, UsdGeom, UsdLux
except ImportError as error:  # pragma: no cover
    raise ImportError("OpenUSD не установлен: pip install usd-core") from error

RULE_SOURCE = "risk_sim/usd_stage.py"
FPS_DEFAULT = 24.0
TOL_SLEW_DEG = 0.5
TOL_POS_M = 0.05

_ZONE_COLORS = {
    "work": ((0.28, 0.58, 1.00), 0.22),
    "edge": ((1.00, 0.25, 0.25), 0.38),
    "passage": ((1.00, 0.80, 0.35), 0.18),
    "storage": ((0.62, 0.30, 0.95), 0.18),
    "crane_hazard": ((1.00, 0.45, 0.15), 0.12),
}
_CLR_COLORS = {"ok": (0.20, 0.85, 0.30), "warn": (0.95, 0.70, 0.05), "violation": (1.0, 0.10, 0.08)}
_CREW_COLORS = [(0.29, 0.56, 0.95), (0.91, 0.53, 0.16), (0.49, 0.91, 0.53),
                (0.87, 0.38, 0.38), (0.70, 0.44, 0.86), (0.55, 0.75, 0.30)]


def log(*a):
    print("[usd_stage]", *a, flush=True)


def _sun_direction(dt: datetime, lat_deg: float, lon_deg: float, tz_hours: float):
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


from risk_sim.clearance import classify_load as _classify_load  # noqa: E402


def _load_dims(kind: str, area_m2: float = 3.0) -> tuple[float, float, float]:
    """Полные габариты (L, W, H) по типу; панель — из площади."""
    if kind == "rebar":
        return (12.0, 0.8, 0.8)
    if kind == "bucket":
        return (2.0, 2.0, 2.0)
    if kind == "section":
        return (3.0, 1.5, 1.5)
    w = math.sqrt(max(0.5, area_m2) / 2.0)
    return (2.0 * w, w, 2.5)


def _prim_name(raw: str) -> str:
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(raw)) or "X"


def _box(stage: Usd.Stage, path: str, size: tuple[float, float, float],
         center: tuple[float, float, float], color=None, opacity: float | None = None):
    mesh = UsdGeom.Mesh.Define(stage, path)
    hx, hy, hz = size[0] / 2, size[1] / 2, size[2] / 2
    pts = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
           (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (3, 7, 6, 2), (0, 4, 7, 3), (1, 2, 6, 5)]
    mesh.CreatePointsAttr([Gf.Vec3f(*p) for p in pts])
    mesh.CreateFaceVertexCountsAttr([4] * 6)
    mesh.CreateFaceVertexIndicesAttr([i for f in faces for i in f])
    mesh.CreateExtentAttr([Gf.Vec3f(hx, hy, hz)])
    xf = UsdGeom.XformCommonAPI(mesh.GetPrim())
    xf.SetTranslate(center)
    if color is not None:
        pv = UsdGeom.PrimvarsAPI(mesh.GetPrim())
        pv.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, "constant") \
          .Set([Gf.Vec3f(*color)])
        if opacity is not None:
            pv.CreatePrimvar("displayOpacity", Sdf.ValueTypeNames.FloatArray, "constant") \
              .Set([opacity])
    return mesh


def _cylinder(stage: Usd.Stage, path: str, radius: float, height: float,
              center: tuple[float, float, float], color=None):
    cyl = UsdGeom.Cylinder.Define(stage, path)
    cyl.CreateRadiusAttr(radius)
    cyl.CreateHeightAttr(height)
    cyl.CreateExtentAttr([Gf.Vec3f(radius, radius, height / 2)])
    UsdGeom.XformCommonAPI(cyl.GetPrim()).SetTranslate(center)
    if color is not None:
        pv = UsdGeom.PrimvarsAPI(cyl.GetPrim())
        pv.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, "constant") \
          .Set([Gf.Vec3f(*color)])
    return cyl


def _export_glb_building(stage: Usd.Stage, glb_path: Path, parent: str) -> int:
    import trimesh
    scene = trimesh.load(str(glb_path), force="scene")
    count = 0
    for key, geom in scene.geometry.items():
        if not isinstance(geom, trimesh.Trimesh) or len(geom.faces) == 0:
            continue
        name = _prim_name(key)
        mesh = UsdGeom.Mesh.Define(stage, f"{parent}/{name}")
        mesh.CreatePointsAttr([Gf.Vec3f(*v) for v in np.asarray(geom.vertices, dtype=np.float64)])
        mesh.CreateFaceVertexCountsAttr([3] * len(geom.faces))
        mesh.CreateFaceVertexIndicesAttr(np.asarray(geom.faces, dtype=np.int32).ravel().tolist())
        count += 1
    return count


def export_stage(bundle_path: str | Path, output: str | Path,
                 fps: float = FPS_DEFAULT) -> dict[str, Any]:
    bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    tl = bundle["timeline"]
    frames = tl["frames"]
    crane = tl["crane"]
    n_frames = len(frames)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)

    stage = Usd.Stage.CreateNew(str(out))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.SetTimeCodesPerSecond(fps)
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(max(1, n_frames - 1))

    root = UsdGeom.Xform.Define(stage, "/RiskSim")
    Usd.ModelAPI(root.GetPrim()).SetKind(Kind.Tokens.assembly)
    root.GetPrim().SetCustomDataByKey("project", tl["meta"].get("project", ""))
    root.GetPrim().SetCustomDataByKey("bundle", str(bundle_path))
    site_x0 = -60.0
    site_x1 = 150.0
    site_y0 = -60.0
    site_y1 = 110.0

    # --- земля ---
    _box(stage, "/RiskSim/Site/Ground", (site_x1 - site_x0, site_y1 - site_y0, 0.35),
         ((site_x0 + site_x1) / 2, (site_y0 + site_y1) / 2, -0.175), color=(0.30, 0.22, 0.14))
    _box(stage, "/RiskSim/Site/Hardstand", (150.0, 110.0, 0.25), (45.0, 25.0, 0.125),
         color=(0.40, 0.40, 0.39))

    # --- здание ---
    mx, my = crane["mast"]["x"], crane["mast"]["y"]
    top_z = crane["mast"]["top_z"]
    head_z = crane["jib"].get("head_z", top_z)
    jl = crane["jib"]["length_m"]
    cjl = crane["jib"].get("counter_jib_m", jl / 3)
    built = 0
    glb = Path(bundle_path).parent / "model.glb"
    if not glb.is_file():
        glb = Path(bundle.get("context", {}).get("data_dir", "data/sample")) / "model.glb"
    if glb.is_file():
        built = _export_glb_building(stage, glb, "/RiskSim/Building")
    log(f"здание: {built} мешей из {glb.name if glb.is_file() else '—'}")

    # --- зоны ---
    for z in tl.get("zones", []):
        color, opacity = _ZONE_COLORS.get(z.get("type", "work"), ((0.5, 0.5, 0.5), 0.2))
        h = min(max(0.8, z["z_top"] - z["z_floor"]), 3.0)
        _box(stage, f"/RiskSim/Zones/{_prim_name(z['id'])}",
             (z["x_max"] - z["x_min"], z["y_max"] - z["y_min"], h),
             ((z["x_min"] + z["x_max"]) / 2, (z["y_min"] + z["y_max"]) / 2,
              z["z_floor"] + h / 2), color=color, opacity=opacity)

    # --- кран ---
    crane_xf = UsdGeom.Xform.Define(stage, "/RiskSim/Crane")
    UsdGeom.XformCommonAPI(crane_xf.GetPrim()).SetTranslate((mx, my, 0.0))
    _cylinder(stage, "/RiskSim/Crane/Mast", 1.2, top_z, (0, 0, top_z / 2), color=(0.93, 0.52, 0.05))

    slew = UsdGeom.Xform.Define(stage, "/RiskSim/Crane/Slew")
    slew_prim = slew.GetPrim()
    slew_xf = UsdGeom.XformCommonAPI(slew_prim)
    slew_xf.SetTranslate((0, 0, top_z))
    jib = _box(stage, "/RiskSim/Crane/Slew/Jib", (jl, 0.25, 0.55), (jl / 2, 0, 0),
               color=(0.93, 0.52, 0.05))
    _box(stage, "/RiskSim/Crane/Slew/CounterJib", (cjl, 0.3, 0.6), (-cjl / 2, 0, 0),
         color=(0.93, 0.52, 0.05))
    _box(stage, "/RiskSim/Crane/Slew/Counterweight", (3.2, 3.2, 2.0), (-cjl + 1.6, 0, 1.6),
         color=(0.60, 0.58, 0.55))
    trolley = _box(stage, "/RiskSim/Crane/Slew/Trolley", (1.6, 1.4, 0.5), (20.0, 0, -0.3),
                   color=(0.62, 0.64, 0.66))
    hook = UsdGeom.Xform.Define(stage, "/RiskSim/Crane/Slew/Trolley/Hook")
    hook_xf = UsdGeom.XformCommonAPI(hook.GetPrim())
    _cylinder(stage, "/RiskSim/Crane/Slew/Trolley/Hook/Rope", 0.04, 1.0, (0, 0, -0.5),
              color=(0.15, 0.14, 0.13))

    # --- грузы: один Xform на тип, visibility по кадрам ---
    kinds = sorted({_classify_load(lf.get("load_name", "")) for lf in bundle.get("lifts", [])})
    load_xf = UsdGeom.Xform.Define(stage, "/RiskSim/Loads")
    load_prims: dict[str, UsdGeom.Xform] = {}
    for kind in kinds:
        L, W, H = _load_dims(kind)
        p = UsdGeom.Xform.Define(stage, f"/RiskSim/Loads/Load_{kind}")
        _box(stage, f"/RiskSim/Loads/Load_{kind}/Body", (L, W, H), (0, 0, -H / 2),
             color=(0.85, 0.42, 0.10))
        load_prims[kind] = p

    # --- клиренс ---
    clr = bundle.get("clearances") or {}
    for idx, rec in enumerate(clr.get("per_frame", [])):
        pt = rec.get("point") or (0, 0, 0)
        _box(stage, f"/RiskSim/Clearance/C{idx:04d}", (0.35, 0.35, 0.35),
             tuple(float(v) for v in pt),
             color=_CLR_COLORS.get(rec.get("status", "ok")))

    # --- бригады ---
    crew_tracks = (bundle.get("agents") or {}).get("tracks") or []
    crews_meta = {c["crew_id"]: c for c in tl.get("crews", [])}
    t_index = {f["t"]: i for i, f in enumerate(frames)}
    for c in crew_tracks:
        cid = _prim_name(c.get("crew_id", "?"))
        size = int(crews_meta.get(c.get("crew_id"), {}).get("size", 1))
        color = _CREW_COLORS[hash(c.get("crew_id", "")) % len(_CREW_COLORS)]
        for member in range(min(size, 8)):
            path = f"/RiskSim/Crews/{cid}_{member:02d}"
            cap = UsdGeom.Capsule.Define(stage, path)
            cap.CreateRadiusAttr(0.32)
            cap.CreateHeightAttr(1.4)
            cap.CreateExtentAttr([Gf.Vec3f(0.32, 0.32, 1.0)])
            pv = UsdGeom.PrimvarsAPI(cap.GetPrim())
            pv.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, "constant") \
              .Set([Gf.Vec3f(*color)])
            xfc = UsdGeom.XformCommonAPI(cap.GetPrim())
            dx = ((member % 3) - 1) * 1.15
            dy = ((member // 3) - 1) * 1.15
            for pt in c.get("points", []):
                fi = t_index.get(pt.get("t"))
                if fi is None:
                    continue
                xfc.SetTranslate((float(pt["x"]) + dx, float(pt["y"]) + dy,
                                  float(pt["z"]) + 0.9), Usd.TimeCode(fi))

    # --- свет: купол + солнце NOAA ---
    UsdLux.DomeLight.Define(stage, "/RiskSim/Lights/Sky").CreateIntensityAttr(0.55)
    sun = UsdLux.DistantLight.Define(stage, "/RiskSim/Lights/Sun")
    sun.CreateIntensityAttr(35.0)
    sun.CreateAngleAttr(0.55)
    sun_xf = UsdGeom.XformCommonAPI(sun.GetPrim())
    lat = 41.0
    lon = 29.0
    tz = 3.0
    for i, f in enumerate(frames):
        el, az = _sun_direction(datetime.strptime(f["t"], "%Y-%m-%d %H:%M"), lat, lon, tz)
        if el > math.radians(-1.0):
            UsdGeom.Imageable(sun.GetPrim()).GetVisibilityAttr().Set(UsdGeom.Tokens.inherited, Usd.TimeCode(i))
            sun_xf.SetRotate(Gf.Vec3f(math.degrees(el) - 90.0, 0.0, -math.degrees(az)), UsdGeom.XformCommonAPI.RotationOrderXYZ, Usd.TimeCode(i))
        else:
            UsdGeom.Imageable(sun.GetPrim()).GetVisibilityAttr().Set(UsdGeom.Tokens.invisible, Usd.TimeCode(i))

    # --- камеры ---
    top_level = max(float(z["z_floor"]) for z in tl["zones"])
    active = [z for z in tl["zones"] if float(z["z_floor"]) >= top_level - 1
              and z.get("type") != "crane_hazard"]
    fx = (min(z["x_min"] for z in active) + max(z["x_max"] for z in active)) / 2
    fy = (min(z["y_min"] for z in active) + max(z["y_max"] for z in active)) / 2
    fz = top_level + 2
    tower_top = head_z + 16.0
    radius = max(320.0, tower_top * 1.8)
    cam = UsdGeom.Camera.Define(stage, "/RiskSim/Cameras/Overview")
    cam.CreateFocalLengthAttr(30.0)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.1, 8000.0))
    cam_xf = UsdGeom.XformCommonAPI(cam.GetPrim())

    def _look_at_euler(eye, target):
        d = np.asarray(target, dtype=np.float64) - np.asarray(eye, dtype=np.float64)
        d = d / max(1e-9, np.linalg.norm(d))
        pitch = math.asin(max(-1.0, min(1.0, d[2])))
        yaw = math.atan2(d[1], d[0])
        return (math.degrees(pitch) - 90.0, 0.0, math.degrees(yaw) - 90.0)

    n = max(1, n_frames - 1)
    for fi, ang, h in ((0, 35.0, 0.13), (n // 3, -25.0, 0.09),
                       (2 * n // 3, 65.0, 0.18), (n, 35.0, 0.13)):
        rad = math.radians(ang)
        eye = (fx + radius * math.cos(rad), fy - radius * math.sin(rad), tower_top * h)
        cam_xf.SetTranslate(eye, Usd.TimeCode(fi))
        cam_xf.SetRotate(Gf.Vec3f(*_look_at_euler(eye, (fx, fy, tower_top * 0.44))), UsdGeom.XformCommonAPI.RotationOrderXYZ, Usd.TimeCode(fi))
    for name, pos, lens in (("Crane", (mx - jl * 1.8, my + jl * 1.1, fz - 95), 50.0),
                            ("Ground", (fx - 30, fy - 60, 1.7), 28.0)):
        c = UsdGeom.Camera.Define(stage, f"/RiskSim/Cameras/{name}")
        c.CreateFocalLengthAttr(lens)
        c.CreateClippingRangeAttr(Gf.Vec2f(0.1, 8000.0))
        tgt = (mx, my, head_z - 32.0) if name == "Crane" else (fx, fy, fz)
        xf = UsdGeom.XformCommonAPI(c.GetPrim())
        xf.SetTranslate(pos)
        xf.SetRotate(_look_at_euler(pos, tgt))

    # --- покадровые timeSamples ---
    r_min = max(4.0, crane["jib"].get("min_radius_m", 4.0))
    r_max = jl
    trolley_xf = UsdGeom.XformCommonAPI(trolley.GetPrim())
    ring_h = float((bundle.get("clearances") or {}).get("ring_h", 1.56) or 1.56)
    kind_status: dict[int, str] = {}
    for i, f in enumerate(frames):
        cr = f.get("crane") or {}
        loads = f.get("loads") or []
        if cr and cr.get("slew_deg") is not None:
            slew_xf.SetRotate(Gf.Vec3f(0.0, 0.0, float(cr["slew_deg"])), UsdGeom.XformCommonAPI.RotationOrderXYZ, Usd.TimeCode(i))
            r = min(max(float(cr.get("radius_m", 20.0)), r_min), r_max)
            trolley_xf.SetTranslate((r, 0, -0.3), Usd.TimeCode(i))
            hz = float(cr.get("hook_z_m", top_z - 20))
            hook_xf.SetTranslate((0, 0, hz - (top_z + 0.25)), Usd.TimeCode(i))
        if loads:
            l0 = loads[0]
            kind = _classify_load(l0.get("load_name", ""))
            kind_status[i] = kind
            p = load_prims.get(kind)
            if p is not None:
                UsdGeom.XformCommonAPI(p.GetPrim()).SetTranslate(
                    (float(l0["x"]), float(l0["y"]), float(l0["z"])), Usd.TimeCode(i))

    # visibility грузов: виден только активный тип
    prev_kind: str | None = None
    sorted_frames = sorted(kind_status.items())
    for i in range(n_frames):
        kind = kind_status.get(i)
        for k, p in load_prims.items():
            visible = (k == kind)
            vis_attr = UsdGeom.Imageable(p.GetPrim()).GetVisibilityAttr()
            cur = UsdGeom.Tokens.inherited if visible else UsdGeom.Tokens.invisible
            if k == prev_kind if False else True:
                vis_attr.Set(cur, Usd.TimeCode(i))
        prev_kind = kind

    stage.GetRootLayer().Save()
    log(f"stage: {out} ({n_frames} кадров, {len(kinds)} типов грузов, "
        f"кадров с клиренсом {len(clr.get('per_frame', []))})")
    return {"stage": str(out), "frames": n_frames, "fps": fps,
            "kinds": kinds, "building_meshes": built}


def validate_stage(bundle_path: str | Path, stage_path: str | Path,
                   sample_step: int = 100) -> dict[str, Any]:
    """Инженерная сверка USD-stage с bundle: slew/translate на выборочных кадрах."""
    bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    frames = bundle["timeline"]["frames"]
    stage = Usd.Stage.Open(str(stage_path))
    errors: list[str] = []
    checks: list[dict[str, Any]] = []

    if UsdGeom.GetStageUpAxis(stage) != UsdGeom.Tokens.z:
        errors.append("up axis != Z")
    if abs(stage.GetTimeCodesPerSecond() - FPS_DEFAULT) > 1e-6:
        errors.append(f"timeCodesPerSecond != {FPS_DEFAULT}")

    slew_prim = stage.GetPrimAtPath("/RiskSim/Crane/Slew")
    load_kinds = sorted({str(p.GetName()).replace("Load_", "")
                         for p in stage.GetPrimAtPath("/RiskSim/Loads").GetAllChildren()})
    checked = 0
    worst_slew, worst_pos = 0.0, 0.0
    for i in range(0, len(frames), sample_step):
        f = frames[i]
        cr = f.get("crane") or {}
        tc = Usd.TimeCode(i)
        if cr and cr.get("slew_deg") is not None:
            xf = UsdGeom.Xformable(slew_prim)
            ops = {op.GetOpName(): op for op in xf.GetOrderedXformOps()}
            rot = ops.get("xformOp:rotateXYZ")
            if rot is None:
                errors.append(f"кадр {i}: нет rotateXYZ на Slew")
                continue
            euler = rot.Get(tc)
            measured = float(euler[2]) % 360.0
            expected = float(cr["slew_deg"]) % 360.0
            d = abs((measured - expected + 180.0) % 360.0 - 180.0)
            worst_slew = max(worst_slew, d)
            if d > TOL_SLEW_DEG:
                errors.append(f"кадр {i}: slew {measured:.2f}° != {expected:.2f}°")
        loads = f.get("loads") or []
        if loads:
            kind = _classify_load(loads[0].get("load_name", ""))
            lp = stage.GetPrimAtPath(f"/RiskSim/Loads/Load_{kind}")
            if not lp.IsValid():
                errors.append(f"кадр {i}: нет прима Load_{kind}")
                continue
            tr = {op.GetOpName(): op for op in UsdGeom.Xformable(lp).GetOrderedXformOps()}
            tr_op = tr.get("xformOp:translate")
            if tr_op is None:
                errors.append(f"кадр {i}: нет translate на Load_{kind}")
                continue
            v = tr_op.Get(tc)
            exp = (float(loads[0]["x"]), float(loads[0]["y"]), float(loads[0]["z"]))
            d = math.dist((v[0], v[1], v[2]), exp)
            worst_pos = max(worst_pos, d)
            if d > TOL_POS_M:
                errors.append(f"кадр {i}: груз {d:.3f} м != {exp}")
        checked += 1

    report = {
        "stage": str(stage_path),
        "frames_checked": checked,
        "worst_slew_deg": round(worst_slew, 4),
        "worst_load_m": round(worst_pos, 4),
        "tolerances": {"slew_deg": TOL_SLEW_DEG, "load_m": TOL_POS_M},
        "errors": errors[:20],
        "verdict": "PASS" if not errors else "FAIL",
    }
    out = Path(stage_path).parent / "scene_stage_validation.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"валидация stage: {report['verdict']} ({checked} кадров, "
        f"slew {report['worst_slew_deg']}°, груз {report['worst_load_m']} м) → {out}")
    return report
