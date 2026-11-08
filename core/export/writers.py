"""
core/export/writers.py — запись результатов в файлы.

Формат JSON между ядром и показом — главный приём архитектуры (раздел 3).
Здесь он зафиксирован: один `scene_bundle.json` содержит ВСЁ, что нужно
Blender, Three.js и Streamlit. Добавить Unreal/Omniverse = написать ещё один
читатель этого файла, не трогая расчёты.

Файлы:
  results/context_info.json    что за данные, версия правил, предупреждения
  results/conflicts.csv        таблица для ПТО (открывается в Excel)
  results/findings.json        то же машиночитаемое + метрики
  results/lift_report_*.json   физика каждого подъёма + трек раскачки
  results/lift_summary.csv     сводная таблица по подъёмам
  results/agent_events.csv     кто, где, когда и под чем
  results/agent_tracks.json    траектории бригад для анимации
  results/mc_lifts.json        Monte Carlo подъёмов
  results/mc_schedule.json     Monte Carlo графика
  results/scene_bundle.json    единый пакет для визуализации
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
def ensure_results_dir(path: str | Path = "results") -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


class _Enc(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            v = float(o)
            return None if not np.isfinite(v) else round(v, 6)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (pd.Timestamp,)):
            return o.strftime("%Y-%m-%d %H:%M")
        if isinstance(o, float) and not np.isfinite(o):
            return None
        if hasattr(o, "as_dict"):
            return o.as_dict()
        return super().default(o)


def dump_json(obj: Any, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, cls=_Enc)
    return p


# ---------------------------------------------------------------------------
def write_context_info(ctx, outdir: Path) -> Path:
    return dump_json(ctx.info(), outdir / "context_info.json")


def write_findings(findings: Sequence[Any], outdir: Path) -> tuple[Path, Path]:
    df = pd.DataFrame([f.as_row() for f in findings])
    csv_path = outdir / "conflicts.csv"
    if len(df):
        cols = ["start", "end", "duration_min", "severity", "rule_id", "rule_title",
                "zone_ids", "task_ids", "crew_ids", "people_exposed", "exposure_person_min",
                "lift_id", "x", "y", "z", "message", "source", "metrics"]
        cols = [c for c in cols if c in df.columns]
        df = df[cols].sort_values(["severity", "start"],
                                  key=lambda s: s.map({"critical": 0, "high": 1, "medium": 2, "low": 3})
                                  if s.name == "severity" else s,
                                  ascending=[True, True])
    df.to_csv(csv_path, index=False)
    js = dump_json({"count": len(findings), "findings": [f.as_dict() for f in findings]},
                   outdir / "findings.json")
    return csv_path, js


def write_lift_reports(assessments: Sequence[Any], outdir: Path,
                       include_track: bool = True) -> list[Path]:
    paths = []
    rows = []
    for a in assessments:
        d = a.as_dict()
        if not include_track and "track" in d.get("swing", {}):
            d["swing"]["track"] = f"[{len(d['swing']['track'])} точек — см. include_track=True]"
        paths.append(dump_json(d, outdir / f"lift_report_{a.lift.lift_id}.json"))
        s, sw = a.summary, a.swing
        rows.append({
            "lift_id": a.lift.lift_id, "task_id": a.task.task_id, "load_name": a.lift.load_name,
            "mass_kg": a.lift.mass_kg, "area_m2": a.lift.area_m2, "radius_m": a.lift.radius_m,
            "hook_height_m": a.lift.hook_height_m, "daf": a.daf,
            "wind_limit_ms": a.lift.wind_limit_ms,
            "v_ref_max_ms": s.get("v_ref_max_ms"), "v_work_max_ms": s.get("v_work_max_ms"),
            "wind_force_max_N": round(s.get("wind_force_max_N", 0), 1),
            "sling_tension_max_N": round(s.get("sling_tension_max_N", 0), 1),
            "sling_utilization_max": round(s.get("sling_utilization_max", 0), 3),
            "crane_capacity_kg": round(a.crane_capacity_kg, 0),
            "crane_utilization_max": (round(s["crane_utilization_max"], 3)
                                      if s.get("crane_utilization_max") is not None else None),
            "tagline_people": a.lift.tagline_people,
            "tagline_capacity_N": a.limits.get("tagline_capacity_N"),
            "tagline_deficit_max_N": round(s.get("tagline_deficit_max_N", 0), 1),
            "safe_wind_ms_by_tagline": a.limits.get("safe_wind_ms_by_tagline_at_work_height"),
            "swing_max_offset_m": sw.get("max_offset_m"),
            "swing_steady_offset_m": sw.get("steady_offset_m"),
            "swing_max_theta_deg": sw.get("max_theta_deg"),
            "swing_period_s": sw.get("natural_period_s"),
            "violations": len(a.violations),
            "violation_rules": ";".join(sorted({v["rule_id"] for v in a.violations})),
            "warnings": len(a.warnings),
        })
        if len(a.series):
            a.series.to_csv(outdir / f"lift_series_{a.lift.lift_id}.csv", index=False)
    pd.DataFrame(rows).to_csv(outdir / "lift_summary.csv", index=False)
    return paths


def write_timeline(timeline: dict[str, Any], outdir: Path) -> Path:
    return dump_json(timeline, outdir / "scene_bundle.json")


def write_agent_outputs(res: Any, outdir: Path, track_every: int = 1) -> tuple[Path, Path]:
    df = res.events_df()
    p1 = outdir / "agent_events.csv"
    df.to_csv(p1, index=False)
    p2 = dump_json({"summary": res.summary(),
                    "tracks": res.tracks_json(every=track_every)},
                   outdir / "agent_tracks.json")
    return p1, p2


def write_mc_outputs(lift_mc: Sequence[Any], sched_mc: Any, outdir: Path) -> list[Path]:
    paths = []
    for r in lift_mc:
        d = r.as_dict()
        d.pop("samples", None)  # массивы по N=5000 в отчёт не кладём
        paths.append(dump_json(d, outdir / f"mc_lift_{r.lift_id}.json"))
    if lift_mc:
        rows = []
        for r in lift_mc:
            rows.append({"lift_id": r.lift_id, "n": r.n, "method": r.method,
                         **{k: round(v, 4) for k, v in r.probabilities.items()}})
        p = outdir / "mc_lift_probability.csv"
        pd.DataFrame(rows).to_csv(p, index=False)
        paths.append(p)
        # кривые «ветер -> вероятность» одним файлом для графиков
        dump_json({r.lift_id: r.safe_wind_curve for r in lift_mc}, outdir / "mc_safe_wind_curves.json")
        paths.append(outdir / "mc_safe_wind_curves.json")
    if sched_mc is not None:
        p = outdir / "mc_schedule_pairs.csv"
        sched_mc.pair_probabilities.to_csv(p, index=False)
        dump_json(sched_mc.as_dict(), outdir / "mc_schedule.json")
        paths += [p, outdir / "mc_schedule.json"]
    return paths


def write_scene_bundle(timeline: dict[str, Any], findings: Sequence[Any],
                       lift_assessments: Sequence[Any], agent_res: Any,
                       mc_lift: Sequence[Any], sched_mc: Any, ctx, outdir: Path,
                       clearance: Any = None) -> Path:
    """Единый пакет для визуализации и панели: всё, что нужно фронту."""
    bundle = {
        "schema_version": timeline.get("schema_version", 1),
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "context": ctx.info(),
        "timeline": timeline,
        "findings": [f.as_dict() for f in findings],
        "findings_summary": _findings_summary(findings),
        "lifts": [a.as_dict() for a in lift_assessments],
        "agents": {"summary": agent_res.summary(),
                   "tracks": agent_res.tracks_json(every=1),
                   "events": [e.as_row() for e in agent_res.events]} if agent_res else None,
        "monte_carlo": {
            "lifts": [{k: v for k, v in r.as_dict().items() if k != "samples"} for r in (mc_lift or [])],
            "schedule": sched_mc.as_dict() if sched_mc is not None else None,
        },
        "clearances": clearance.as_dict() if clearance is not None else None,
        "rules": ctx.cfg.as_dict(),
    }
    from risk_sim.model import load_model_metadata
    model_metadata = load_model_metadata(ctx.data_dir)
    if model_metadata:
        import shutil
        bundle["model"] = model_metadata
        shutil.copy2(ctx.data_dir / model_metadata["glb_file"], outdir / "model.glb")
    return dump_json(bundle, outdir / "scene_bundle.json")


def _findings_summary(findings: Sequence[Any]) -> dict[str, Any]:
    if not findings:
        return {"total": 0}
    df = pd.DataFrame([f.as_row() for f in findings])
    by_rule = df.groupby(["rule_id", "rule_title", "severity", "source"]).agg(
        n=("message", "size"), people=("people_exposed", "max"),
        person_min=("exposure_person_min", "sum")).reset_index()
    by_day = df.assign(day=pd.to_datetime(df["start"], errors="coerce").dt.strftime("%Y-%m-%d")) \
               .groupby("day").size().to_dict()
    by_level = df.assign(level=df["zone_ids"].str.split(";").str[0].str.extract(r"(L\d+|G)")[0]) \
                 .groupby("level").size().to_dict()
    return {
        "total": int(len(df)),
        "by_severity": df["severity"].value_counts().to_dict(),
        "by_rule": by_rule.to_dict(orient="records"),
        "by_day": {k: int(v) for k, v in sorted(by_day.items()) if k},
        "by_level": {k: int(v) for k, v in by_level.items() if k},
        "top_findings": df.nlargest(10, "exposure_person_min")[
            ["start", "severity", "rule_id", "zone_ids", "task_ids", "people_exposed", "message"]
        ].to_dict(orient="records"),
        "people_exposed_max": int(df["people_exposed"].max()),
        "person_minutes_total": round(float(df["exposure_person_min"].sum()), 1),
    }


def write_all(ctx, findings, lift_assessments, timeline, agent_res, mc_lift, sched_mc,
              outdir: str | Path = "results", clearance: Any = None) -> dict[str, Any]:
    """Пишет весь набор результатов и возвращает список путей."""
    out = ensure_results_dir(outdir)
    written: dict[str, Any] = {}
    written["context_info"] = str(write_context_info(ctx, out))
    c, j = write_findings(findings, out)
    written["conflicts_csv"], written["findings_json"] = str(c), str(j)
    written["lift_reports"] = [str(p) for p in write_lift_reports(lift_assessments, out)]
    written["agent"] = [str(p) for p in write_agent_outputs(agent_res, out)] if agent_res else []
    written["mc"] = [str(p) for p in write_mc_outputs(mc_lift, sched_mc, out)]
    written["timeline"] = str(write_timeline(timeline, out))
    written["scene_bundle"] = str(write_scene_bundle(timeline, findings, lift_assessments,
                                                     agent_res, mc_lift, sched_mc, ctx, out,
                                                     clearance=clearance))
    return written
