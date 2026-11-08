from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import DISCLAIMER, __version__
from .agents import build_agent_timeline
from .conflicts import conflicts_frame, detect_conflicts, risk_heatmap
from .io import InputError, load_inputs
from .model import load_model_metadata
from .monte_carlo import schedule_monte_carlo, wind_monte_carlo
from .physics import sensitivity_analysis, simulate_lift, validate_physics


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, np.ndarray):
        return _clean(value.tolist())
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return _clean(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(_clean(value), stream, ensure_ascii=False, indent=2, allow_nan=False)


def _source_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir()) if path.is_file()
    }


def run_simulation(data_dir: str | Path, output_dir: str | Path, viewer_dir: str | Path | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    inputs = load_inputs(data_dir)
    model_metadata = load_model_metadata(inputs.data_dir)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parents[1]
    viewer = Path(viewer_dir).resolve() if viewer_dir else project_root / "viewer"
    scenario_config = inputs.rules["scenario"]
    scenario_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(scenario_config["id"]))
    generated_at = datetime.now().astimezone().isoformat()

    events = detect_conflicts(inputs.works, inputs.zones, inputs.lifts, inputs.wind, inputs.rules)
    frame = conflicts_frame(events)
    frame.to_csv(output / "conflicts.csv", index=False)
    heatmap = risk_heatmap(events)
    heatmap.to_csv(output / "risk_heatmap.csv", index=False)

    validation = validate_physics(inputs.rules["physics"])
    write_json(output / "validation.json", validation)
    if not validation["all_passed"]:
        raise RuntimeError("Проверка физики на известных случаях не пройдена; смотрите validation.json")

    lift_results = []
    wind_summaries = []
    sample_frames = []
    for lift in inputs.lifts.itertuples():
        lift_series = inputs.lifts.loc[lift.Index]
        matching = inputs.works[inputs.works.task_id == lift.task_id]
        if len(matching) != 1:
            raise InputError(f"{lift.lift_id}: подъём должен ссылаться ровно на одну работу")
        for field in ("rope_length_m", "tagline_capacity_n", "tagline_stiffness_n_per_m"):
            if field not in inputs.lifts or pd.isna(getattr(lift, field)):
                raise InputError(f"{lift.lift_id}: для расчёта обязательна колонка {field}")
        result = simulate_lift(lift_series, matching.iloc[0], inputs.wind, inputs.rules["physics"])
        result["sensitivity"] = sensitivity_analysis(lift_series, result["summary"], inputs.rules["physics"])
        summary, samples = wind_monte_carlo(
            lift_series, matching.iloc[0], inputs.wind,
            inputs.rules["physics"], inputs.rules["monte_carlo"],
        )
        lift_results.append({"input": lift_series.to_dict(), **result})
        wind_summaries.append(summary)
        sample_frames.append(samples)

    schedule_summary = schedule_monte_carlo(inputs.works, inputs.zones, inputs.lifts, inputs.rules["monte_carlo"])
    pd.concat(sample_frames, ignore_index=True).to_csv(output / "monte_carlo_samples.csv", index=False)
    monte_carlo = {"wind_and_lift": wind_summaries, "schedule": schedule_summary}
    write_json(output / "monte_carlo.json", monte_carlo)
    write_json(output / "lift_report.json", {
        "generated_at": generated_at, "disclaimer": DISCLAIMER,
        "assumptions": inputs.rules["assumptions"],
        "lifts": [result["summary"] for result in lift_results],
        "sensitivity": {result["summary"]["lift_id"]: result["sensitivity"] for result in lift_results},
    })
    write_json(output / "lift_track.json", {
        result["summary"]["lift_id"]: result["track"] for result in lift_results
    })

    timeline = build_agent_timeline(
        inputs.works, inputs.zones, inputs.building,
        inputs.rules["agents"], int(scenario_config.get("timeline_step_s", 60)),
    )
    scenario = {
        "schema_version": "1.0",
        "metadata": {
            "scenario_id": scenario_id,
            "name": scenario_config["name"],
            "model_version": scenario_config["version"],
            "software_version": __version__,
            "generated_at": generated_at,
            "source_type": scenario_config["source_type"],
            "object_name": inputs.rules["report"]["object_name"],
            "author": inputs.rules["report"]["author"],
            "disclaimer": DISCLAIMER,
            "coordinate_system": scenario_config["coordinate_system"],
            "source_sha256": _source_hashes(inputs.data_dir),
        },
        "units": {
            "length": "m", "mass": "kg", "force": "N", "angle": "deg",
            "wind_speed": "m/s", "time": "ISO 8601 local model time",
        },
        "assumptions": inputs.rules["assumptions"],
        "building": inputs.building,
        "model": model_metadata,
        "zones": inputs.zones.to_dict(orient="records"),
        "tasks": inputs.works.to_dict(orient="records"),
        "wind": inputs.wind.to_dict(orient="records"),
        "lifts": lift_results,
        "events": events,
        "risk_heatmap": heatmap.to_dict(orient="records"),
        "monte_carlo": monte_carlo,
        "timeline": timeline,
        "validation": validation,
    }
    scenario["metadata"]["core_runtime_s"] = time.perf_counter() - started
    scenario = _clean(scenario)
    scenario_path = output / f"scenario_{scenario_id}.json"
    write_json(scenario_path, scenario)

    from .report import generate_reports
    report_paths = generate_reports(scenario, output)
    scenario["artifacts"] = {
        "conflicts": "conflicts.csv",
        "risk_heatmap": "risk_heatmap.csv",
        "lift_report": "lift_report.json",
        "lift_track": "lift_track.json",
        "monte_carlo": "monte_carlo.json",
        "validation": "validation.json",
        "report_docx": report_paths["docx"].name,
        "report_pdf": report_paths["pdf"].name,
    }
    if model_metadata:
        scenario["artifacts"]["model_glb"] = "model.glb"
    scenario["metadata"]["total_runtime_s"] = time.perf_counter() - started
    write_json(scenario_path, scenario)
    viewer.mkdir(parents=True, exist_ok=True)
    shutil.copy2(scenario_path, viewer / "scenario.json")
    report_viewer = viewer / "reports"
    report_viewer.mkdir(exist_ok=True)
    for report_path in report_paths.values():
        shutil.copy2(report_path, report_viewer / report_path.name)
    if model_metadata:
        glb_path = inputs.data_dir / model_metadata["glb_file"]
        shutil.copy2(glb_path, output / "model.glb")
        shutil.copy2(glb_path, viewer / "model.glb")
    return {
        "scenario": scenario_path,
        "conflicts": output / "conflicts.csv",
        "report_docx": report_paths["docx"],
        "report_pdf": report_paths["pdf"],
        "viewer": viewer / "index.html",
        "event_count": len(events),
        "runtime_s": time.perf_counter() - started,
    }
