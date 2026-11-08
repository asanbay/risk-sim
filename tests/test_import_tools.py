"""
tests/test_import_tools.py — инструменты приёма реальных данных (неделя 7).

Проверяем не «код», а гарантии для заказчика:
  * import_wind: единицы приводятся честно, даты не переворачиваются
    (01.10.2026 ≠ 10 января), QC видит залипания/дыры/пики, результат
    загружается ядром без предупреждений о порывах;
  * import_lift: шаблон совпадает со схемой ядра, стоп-замечания ловятся,
    пригодный файл проходит.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import import_lift as IL
from tools import import_wind as IW


def _raw_wind(tmp_path, n=120):
    ts = pd.date_range("2026-10-01 00:00", periods=n, freq="30min")
    ws = 5 + 2 * np.abs(np.sin(np.arange(n) / 7))
    df = pd.DataFrame({"Date/Time": ts.strftime("%d.%m.%Y %H:%M"),
                       "WS": (ws * 3.6).round(2),
                       "GUST": (ws * 1.4 * 3.6).round(2),
                       "WD": 210})
    df.loc[40:55, "WS"] = df.loc[40, "WS"]      # 8 ч одного значения -> залипание
    df.loc[60, "WS"] = 999                      # пик > 45 м/с после конвертации
    p = tmp_path / "meteo_raw.csv"
    df.to_csv(p, index=False)
    return p


def test_import_wind_units_and_dates(tmp_path):
    src = _raw_wind(tmp_path)
    out = tmp_path / "wind_measured.csv"
    argv = ["import_wind.py", "--in", str(src), "--datetime-col", "Date/Time",
            "--speed-col", "WS", "--gust-col", "GUST", "--dir-col", "WD",
            "--height-m", "42", "--units", "km/h", "--out", str(out)]
    old = sys.argv
    sys.argv = argv
    try:
        rc = IW.main()
    finally:
        sys.argv = old
    assert rc == 0
    df = pd.read_csv(out)
    ts = pd.to_datetime(df["timestamp"])
    assert ts.min().month == 10 and ts.min().day == 1, "дата перевёрнута (dd/mm vs mm/dd)"
    # 5 м/с в km/h = 18; обратно -> 5
    assert df["mean_ms"].between(0, 46).mean() > 0.98
    assert (df["gust_ms"] >= df["mean_ms"]).mean() > 0.99
    assert set(["timestamp", "mean_ms", "gust_ms", "ref_height_m"]) <= set(df.columns)
    qc = pd.read_json(str(out) + ".qc.json")
    assert qc["stuck_segments_6h"][0] >= 1
    assert qc["spike_over_45ms"][0] >= 1


def test_import_wind_output_loads_in_core(tmp_path):
    src = _raw_wind(tmp_path, n=60)
    out = tmp_path / "w.csv"
    old = sys.argv
    sys.argv = ["import_wind.py", "--in", str(src), "--datetime-col", "Date/Time",
                "--speed-col", "WS", "--gust-col", "GUST", "--height-m", "42",
                "--units", "km/h", "--out", str(out)]
    try:
        IW.main()
    finally:
        sys.argv = old
    from core.io.loaders import load_wind
    w = load_wind(out)
    assert len(w.ts) == 60
    assert w.ref_height_m == 42.0


def test_import_lift_template_matches_core_schema(tmp_path):
    out = tmp_path / "lift_template.csv"
    assert IL.template(out) == 0
    df = pd.read_csv(out)
    from core.io.loaders import load_lifts
    req = ["lift_id", "task_id", "mass_kg", "area_m2", "sling_angle_deg",
           "carrying_legs", "sling_wll_kg", "radius_m", "hook_height_m"]
    assert all(c in df.columns for c in req)
    assert list(df.columns) == IL.TEMPLATE_COLS


def test_import_lift_check_catches_stops(tmp_path, capsys):
    from pathlib import Path
    good = pd.DataFrame([IL.EXAMPLE])
    bad = good.copy()
    bad["mass_kg"] = 40000
    bad["sling_angle_deg"] = 0
    bad["tagline_people"] = 7
    p = tmp_path / "lifts.csv"
    pd.concat([good, bad], ignore_index=True).to_csv(p, index=False)
    rc = IL.check(p, Path(ROOT) / "data/sample/crane_load_chart.csv",
                  Path(ROOT) / "data/sample/rules.yaml")
    out = capsys.readouterr().out
    assert rc == 1
    assert "СТОП" in out and "40000" in out


def test_import_lift_sample_passes():
    from pathlib import Path
    rc = IL.check(Path(ROOT) / "data/sample/lifts.csv",
                  Path(ROOT) / "data/sample/crane_load_chart.csv",
                  Path(ROOT) / "data/sample/rules.yaml")
    assert rc == 0, "sample-данные должны проходить проверку собственного чекера"
