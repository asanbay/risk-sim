"""
viewer/build_viewer.py — сборка самодостаточной HTML-страницы просмотра.

Зачем инлайним данные в HTML, а не грузим fetch-ем:
  * файл можно открыть двойным щелчком и отправить руководству;
  * работает без сервера и без сети (в т.ч. в песочницах и iframe);
  * один файл = одна версия = нечего рассинхронизировать.

Полный scene_bundle.json весит единицы мегабайт (треки раскачки, серия по
каждым 15 минут). Для просмотра он не нужен целиком, поэтому здесь
собирается КОМПАКТНЫЙ бандл: кадры выровнены по сетке таймлайна,
координаты округлены, массивы вместо объектов. Обычно это 100-400 КБ.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def compact_bundle(bundle: dict[str, Any], *, track_every: int = 1,
                   keep_lift_tracks: bool = False) -> dict[str, Any]:
    """Сжимает scene_bundle до размера, пригодного для инлайна в HTML."""
    tl = bundle.get("timeline", {})
    frames = tl.get("frames", [])

    # --- кадры: только то, что рисуется ---------------------------------
    cf = []
    for f in frames:
        cr = f.get("crane") or {}
        cf.append({
            "t": f["t"],
            "w": [f["wind"]["gust_at_ref_ms"], f["wind"]["gust_at_top_ms"], f["wind"]["mean_at_ref_ms"]],
            "c": [cr.get("slew_deg"), cr.get("radius_m"), cr.get("hook_z_m"), cr.get("capacity_kg")]
                 if cr else None,
            "l": [[l["lift_id"], round(l["x"], 2), round(l["y"], 2), round(l["z"], 2),
                   round(l.get("swing_offset_m", 0.0), 2), l.get("phase", "")]
                  for l in f.get("loads", [])],
            "a": f.get("tasks_active", []),
        })

    # --- бригады: прореживаем и выравниваем по кадрам -------------------
    crews_out = []
    for c in bundle.get("agents", {}).get("tracks", []) if bundle.get("agents") else []:
        pts = c["points"][::max(1, track_every)]
        crews_out.append({
            "id": c["crew_id"], "name": c["name"], "size": c["size"], "color": c["color"],
            "p": [[round(p["x"], 1), round(p["y"], 1), round(p["z"], 1),
                   {"working": 2, "transit": 1, "break": 0, "idle": 0}.get(p["state"], 0),
                   p["task"]] for p in pts],
        })

    # --- находки: короткая форма ----------------------------------------
    fnd = []
    for f in bundle.get("findings", []):
        fnd.append({
            "id": f["rule_id"], "sev": f["severity"], "t": f["rule_title"],
            "s": f["start"], "e": f["end"], "z": f["zone_ids"], "tk": f["task_ids"],
            "cr": f["crew_ids"], "n": f["people_exposed"], "m": f["message"],
            "src": f["source"], "p": f.get("location"), "lift": f.get("lift_id") or "",
        })

    # --- подъёмы: сводка + кривые безопасности ---------------------------
    lifts = []
    for l in bundle.get("lifts", []):
        s, sw, lim = l.get("summary", {}), l.get("swing", {}), l.get("limits", {})
        lifts.append({
            "id": l["lift_id"], "task": l["task_id"], "name": l["load_name"],
            "mass": l["input"]["mass_kg"], "area": l["input"]["area_m2"],
            "radius": l["input"]["radius_m"], "hook_z": l["input"]["hook_height_m"],
            "pick": l["input"]["pickup"], "drop": l["input"]["dropoff"],
            "v_work_max": s.get("v_work_max_ms"), "f_max": s.get("wind_force_max_N"),
            "u_sling": s.get("sling_utilization_max"), "u_crane": s.get("crane_utilization_max"),
            "tag_def": s.get("tagline_deficit_max_N"), "tag_cap": lim.get("tagline_capacity_N"),
            "safe_v_ref": lim.get("safe_wind_ms_by_tagline_at_ref_height"),
            "offset": sw.get("steady_offset_m"), "theta": sw.get("max_theta_deg"),
            "period": sw.get("natural_period_s"),
            "windows": s.get("safe_windows", [])[:12],
            "viol": [[v["rule_id"], v["severity"], v["message"]] for v in l.get("violations", [])],
            "warn": l.get("warnings", []),
            "track": (sw.get("track") if keep_lift_tracks else None),
        })

    mc = bundle.get("monte_carlo", {}) or {}
    mc_lifts = [{
        "id": m["lift_id"], "n": m["n"], "method": m["method"],
        "p": {k: round(v, 4) for k, v in m.get("probabilities", {}).items()},
        "pct": m.get("percentiles", {}),
        "curve": m.get("safe_wind_curve", []),
    } for m in mc.get("lifts", [])]

    sched = mc.get("schedule") or {}
    pairs = sched.get("top_pairs", [])[:20]

    return {
        "schema": 2,
        "generated": bundle.get("generated_at"),
        "context": bundle.get("context", {}),
        "model": bundle.get("model"),
        "meta": tl.get("meta", {}),
        "crane": tl.get("crane", {}),
        "zones": tl.get("zones", []),
        "tasks": tl.get("tasks", []),
        "crews": tl.get("crews", []),
        "frames": cf,
        "crew_tracks": crews_out,
        "findings": fnd,
        "findings_summary": bundle.get("findings_summary", {}),
        "lifts": lifts,
        "mc_lifts": mc_lifts,
        "mc_schedule": {"rule_probabilities": sched.get("rule_probabilities", {}),
                        "exposure_person_hours": sched.get("exposure_person_hours", {}),
                        "top_pairs": pairs, "params": sched.get("params", {})},
        "agent_summary": (bundle.get("agents") or {}).get("summary", {}),
        "agent_events": [
            {"rule": e["rule_id"], "sev": e["severity"], "crew": e["crew_id"], "zone": e["zone_id"],
             "s": e["start"], "e": e["end"], "pm": e["person_minutes"], "msg": e["message"],
             "lift": e.get("lift_id", "")}
            for e in (bundle.get("agents") or {}).get("events", [])
        ],
        "rules": (bundle.get("rules") or {}),
    }


def build_viewer(results_dir: str | Path = "results",
                 out_path: str | Path = "viewer/index.html",
                 template: str | Path | None = None,
                 track_every: int = 1) -> Path:
    """Собирает viewer/index.html из results/scene_bundle.json."""
    results = Path(results_dir)
    bundle_path = results / "scene_bundle.json"
    if not bundle_path.exists():
        raise FileNotFoundError(f"Не найден {bundle_path}. Сначала запустите run.py")
    with bundle_path.open(encoding="utf-8") as fh:
        bundle = json.load(fh)

    compact = compact_bundle(bundle, track_every=track_every)
    payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    tpl_path = Path(template) if template else Path(__file__).parent / "template.html"
    html = tpl_path.read_text(encoding="utf-8")
    marker = "/*__BUNDLE__*/null"
    if marker not in html:
        raise ValueError(f"В {tpl_path} нет маркера {marker}")
    html = html.replace(marker, payload)
    # размер бандла — полезно знать, не уехали ли мы в мегабайты
    html = html.replace("/*__BUNDLE_KB__*/0", str(round(len(payload) / 1024.0, 1)))

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--out", default="viewer/index.html")
    ap.add_argument("--track-every", type=int, default=1)
    a = ap.parse_args()
    p = build_viewer(a.results, a.out, track_every=a.track_every)
    print(f"[viewer] {p} ({p.stat().st_size/1024:.0f} КБ)")
