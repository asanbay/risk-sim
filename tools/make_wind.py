#!/usr/bin/env python3
"""
tools/make_wind.py — генератор СИНТЕТИЧЕСКИХ почасовых данных ветра для демо.

В реальном проекте wind.csv берётся из:
  * метеостанции на объекте (лучший вариант),
  * реанализа / архива для площадки,
  * отчёта по ветровому отклику здания (wind engineering report) — там же
    коэффициенты профиля и порывов для высоты работ.

Генератор воспроизводим (seed) и даёт реалистичную структуру:
  Weibull-распределение средней скорости, суточный ход, фронтальный шторм
  на третьи сутки (в демо — 2026-11-19), порыв = mean * gust_factor.

ВАЖНО: измерения обычно приводятся к высоте 10 м (колонка ref_height_m).
Пересчёт на высоту работ делает ядро (core/physics/wind.py), а не этот файл.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def weibull_speed(rng, n, k=2.0, lam=6.2):
    return rng.weibull(k, n) * lam


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/sample/wind.csv")
    ap.add_argument("--start", default="2026-11-16 00:00")
    ap.add_argument("--hours", type=int, default=7 * 24)
    ap.add_argument("--seed", type=int, default=20261116)
    ap.add_argument("--ref-height", type=float, default=10.0)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    ts = pd.date_range(args.start, periods=args.hours, freq="h")
    hours = np.arange(args.hours)

    # 1) базовая скорость: Weibull + автокорреляция (сглаживание соседних часов)
    raw = weibull_speed(rng, args.hours)
    mean = np.copy(raw)
    for _ in range(3):  # простая AR-подобная фильтрация -> плавные фронты
        mean = 0.55 * mean + 0.45 * np.r_[mean[0], mean[:-1]]

    # 2) суточный ход (слабый у земли)
    mean += 1.1 * np.sin(2 * np.pi * (hours - 9) / 24.0)

    # 3) штормовой фронт: 2026-11-19, пик около 14:00
    storm_center = int(np.argmin(np.abs((ts - pd.Timestamp("2026-11-19 14:00")).total_seconds())))
    bump = 9.5 * np.exp(-0.5 * ((hours - storm_center) / 9.0) ** 2)
    mean += bump

    mean = np.clip(mean, 0.4, 26.0)

    # 4) порыв: фактор растёт со скоростью (устойчивость атмосферы падает)
    gust_factor = 1.65 - 0.028 * mean + rng.normal(0, 0.05, args.hours)
    gust = np.clip(mean * np.clip(gust_factor, 1.25, 1.85), mean, None)

    df = pd.DataFrame(
        {
            "timestamp": ts.strftime("%Y-%m-%d %H:%M"),
            "mean_ms": np.round(mean, 2),
            "gust_ms": np.round(gust, 2),
            "ref_height_m": args.ref_height,
            "direction_deg": np.round((225 + 35 * np.sin(2 * np.pi * hours / 53.0) + rng.normal(0, 8, args.hours)) % 360, 1),
            "source": "SYNTHETIC (tools/make_wind.py) — заменить данными метеостанции",
        }
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"[make_wind] {out} : {len(df)} часов, mean {df.mean_ms.min():.1f}–{df.mean_ms.max():.1f} м/с, "
          f"gust max {df.gust_ms.max():.1f} м/с, пик шторма {ts[storm_center]}")


if __name__ == "__main__":
    main()
