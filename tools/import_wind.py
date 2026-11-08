"""
tools/import_wind.py — приём РЕАЛЬНЫХ данных метеостанции (неделя 7 пилота).

    python tools/import_wind.py --in meteo_raw.csv \
        --datetime-col "Date/Time" --speed-col WS --height-m 42 \
        --units km/h --gust-col GUST --dir-col WD \
        --out data/wind_measured.csv

Что делает:
  1. разбирает метки времени (несколько популярных форматов, включая
     «дд.мм.гггг чч:мм» и ISO);
  2. приводит скорость к м/с (m/s | km/h | kt);
  3. приводит к схеме ядра: timestamp, mean_ms, gust_ms, ref_height_m,
     direction_deg, source — именно её читает core.io.loaders.load_wind;
  4. гоняет QC: пропуски, отрицательные, пики > 45 м/с, «залипшие» участки
     (одно значение >= 6 ч), дыры в ряду > 2 ч; пишет <out>.qc.json и печатает
     сводку. QC НЕ отбраковывает файл молча: всё, что исправлено/помечено,
     видно в отчёте.

Если колонки порывов нет — gust_ms НЕ выдумывается: файл помечается
source="...; gust=mean (ЗАНИЖАЕТ РИСК)", и ядро при загрузке повторит
предупреждение. Решение о коэффициенте порыва принимает инженер, а не скрипт.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

UNIT_TO_MS = {"m/s": 1.0, "ms": 1.0, "km/h": 1 / 3.6, "kmh": 1 / 3.6, "kt": 0.514444,
              "knots": 0.514444}
DT_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M",
              "%d.%m.%Y %H:%M:%S", "%Y/%m/%d %H:%M", "%d/%m/%Y %H:%M", None]


def parse_dt(s: pd.Series) -> pd.Series:
    """Сначала ЯВНЫЕ форматы (иначе '01.10.2026' угадается как 10 января)."""
    for fmt in DT_FORMATS:
        out = pd.to_datetime(s, format=fmt, errors="coerce") if fmt else \
            pd.to_datetime(s, errors="coerce", dayfirst=True)
        if out.notna().mean() > 0.98:
            return out
    return pd.to_datetime(s, errors="coerce", dayfirst=True)


def qc_report(df: pd.DataFrame, height_m: float) -> dict:
    rep: dict = {"rows": int(len(df)), "height_m": height_m}
    ts = pd.DatetimeIndex(df["timestamp"])
    rep["span"] = [str(ts.min()), str(ts.max())]
    rep["missing_mean_pct"] = round(float(df["mean_ms"].isna().mean() * 100), 2)
    rep["negative_rows"] = int((df["mean_ms"] < 0).sum())
    rep["spike_over_45ms"] = int((df["mean_ms"] > 45).sum())
    # залипание: то же значение >= 6 ч ПОДРЯД (считаем в времени, не в строках)
    dt = ts.to_series().diff().dt.total_seconds() / 3600
    step_h = float(dt.median()) if len(dt.dropna()) and dt.median() == dt.median() else 0.5
    need = max(2, int(round(6.0 / step_h)))
    stuck, cur = 0, 1
    v = df["mean_ms"].to_numpy()
    for i in range(1, len(v)):
        if v[i] == v[i - 1] and not np.isnan(v[i]):
            cur += 1
        else:
            stuck += cur >= need
            cur = 1
    stuck += cur >= need
    rep["stuck_segments_6h"] = int(stuck)
    rep["gaps_over_2h"] = int((dt > 2).sum())
    rep["median_step_min"] = round(float(dt.median() * 60), 1) if len(dt.dropna()) else None
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--datetime-col", default=None, help="имя колонки времени (иначе первая)")
    ap.add_argument("--speed-col", default=None, help="имя колонки средней скорости")
    ap.add_argument("--gust-col", default=None)
    ap.add_argument("--dir-col", default=None)
    ap.add_argument("--height-m", type=float, required=True,
                    help="высота установки анемометра, м (из паспорта станции)")
    ap.add_argument("--units", default="m/s", choices=sorted(UNIT_TO_MS))
    ap.add_argument("--sep", default=",")
    ap.add_argument("--out", default="data/wind_measured.csv")
    ap.add_argument("--qc-only", action="store_true", help="только QC-отчёт, без записи")
    a = ap.parse_args()

    raw = pd.read_csv(a.src, sep=a.sep)
    dt_col = a.datetime_col or raw.columns[0]
    spd_col = a.speed_col or next((c for c in raw.columns if c != dt_col), raw.columns[1])
    if dt_col not in raw.columns or spd_col not in raw.columns:
        print(f"ОШИБКА: колонки {dt_col}/{spd_col} не найдены; есть: {list(raw.columns)}")
        return 2

    df = pd.DataFrame()
    df["timestamp"] = parse_dt(raw[dt_col].astype(str))
    bad_ts = int(df["timestamp"].isna().sum())
    df["mean_ms"] = pd.to_numeric(raw[spd_col], errors="coerce") * UNIT_TO_MS[a.units]
    if a.gust_col and a.gust_col in raw.columns:
        df["gust_ms"] = pd.to_numeric(raw[a.gust_col], errors="coerce") * UNIT_TO_MS[a.units]
        gust_note = f"gust=measured({a.gust_col})"
    else:
        df["gust_ms"] = df["mean_ms"]
        gust_note = "gust=mean (ЗАНИЖАЕТ РИСК: нет колонки порывов)"
    df["ref_height_m"] = a.height_m
    df["direction_deg"] = (pd.to_numeric(raw[a.dir_col], errors="coerce")
                           if a.dir_col and a.dir_col in raw.columns else np.nan)
    df["source"] = f"MEASURED {Path(a.src).name}; units={a.units}; {gust_note}"

    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    rep = qc_report(df, a.height_m)
    rep["unparsed_timestamps"] = bad_ts
    rep["gust"] = gust_note

    print(json.dumps(rep, ensure_ascii=False, indent=1))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.parent / (out.name + ".qc.json")).write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.qc_only:
        return 0
    df.to_csv(out, index=False)
    print(f"[import_wind] записано {out} ({len(df)} строк)")
    warns = [k for k in ("negative_rows", "spike_over_45ms", "stuck_segments_6h",
                         "gaps_over_2h", "unparsed_timestamps") if rep.get(k)]
    if warns:
        print("[import_wind] ВНИМАНИЕ: есть замечания QC — см. "
              f"{out.parent / (out.name + '.qc.json')}: {warns}")
        print("[import_wind] решение о пригодности ряда принимает инженер, а не скрипт.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
