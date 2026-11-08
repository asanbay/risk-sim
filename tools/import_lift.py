"""
tools/import_lift.py — шаблон и проверка подъёма из ППР (неделя 7 пилота).

    python tools/import_lift.py template [--out data/templates/lift_template.csv]
    python tools/import_lift.py check my_lifts.csv \
        [--crane data/sample/crane_load_chart.csv] [--rules data/sample/rules.yaml]

Проверки (все числа сравниваются с паспортами/ППР, а не «на глаз»):
  * обязательные колонки схемы ядра (core.io.loaders.load_lifts);
  * масса против грузовой характеристики крана на данном вылете
    (интерполяция crane_load_chart.csv): utilisation <= 100%;
  * угол ветви стропа 0 < a <= 90 и предупреждение при a < 60
    (дерейтинг по таблицам производителя, см. rules.yaml physics);
  * carrying_legs <= sling_legs;
  * число людей на оттяжке <= max_tagline_people из rules.yaml;
  * wind_limit_ms > 0 (иначе подъём возьмёт лимит крана — это нужно осознать).

Скрипт НЕ правит файл: печатает список замечаний и код возврата
(0 = можно класть в data/, 1 = есть стоп-замечания). Решение — за инженером.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd
import yaml

REQUIRED = ["lift_id", "task_id", "mass_kg", "area_m2", "sling_angle_deg",
            "carrying_legs", "sling_wll_kg", "radius_m", "hook_height_m"]
TEMPLATE_COLS = REQUIRED + ["load_name", "cd_min", "cd_max", "sling_legs",
                            "pickup_x", "pickup_y", "pickup_z",
                            "dropoff_x", "dropoff_y", "dropoff_z",
                            "tagline_people", "wind_limit_ms", "daf_mode"]
EXAMPLE = {
    "lift_id": "L-101", "task_id": "T-201-LIFT", "mass_kg": 2400, "area_m2": 4.2,
    "sling_angle_deg": 60, "carrying_legs": 2, "sling_wll_kg": 5000, "radius_m": 22,
    "hook_height_m": 246.0, "load_name": "Балка Б-12 (паспорт №…)",
    "cd_min": 1.2, "cd_max": 1.6, "sling_legs": 4,
    "pickup_x": 10, "pickup_y": 50, "pickup_z": 1.5,
    "dropoff_x": 10, "dropoff_y": 30, "dropoff_z": 246.0,
    "tagline_people": 2, "wind_limit_ms": 10, "daf_mode": "normal",
}


def template(out: Path) -> int:
    df = pd.DataFrame([EXAMPLE])[TEMPLATE_COLS]
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"[import_lift] шаблон: {out}")
    print("[import_lift] заполните строки значениями из ППР и паспорта крана, "
          "затем: python tools/import_lift.py check <файл>")
    return 0


def check(path: Path, crane_chart: Path, rules: Path) -> int:
    df = pd.read_csv(path)
    problems: list[str] = []
    notes: list[str] = []
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        print(f"СТОП: не хватает колонок: {missing}")
        return 1

    chart = pd.read_csv(crane_chart).sort_values("radius_m")
    max_people = 4
    if rules.exists():
        ry = yaml.safe_load(rules.read_text(encoding="utf-8"))
        max_people = int((ry.get("people") or {}).get("max_tagline_people", 4))

    stop = 0
    for _, r in df.iterrows():
        lid = str(r["lift_id"])
        m, rad = float(r["mass_kg"]), float(r["radius_m"])
        c = _interp(rad, chart)
        util = m / c if c > 0 else math.inf
        if util > 1.0:
            problems.append(f"{lid}: масса {m:.0f} кг > грузоподъёмности {c:.0f} кг "
                            f"на вылете {rad:.0f} м (исп. {util:.0%}) — СТОП")
            stop += 1
        elif util > 0.85:
            notes.append(f"{lid}: использование ГПМ {util:.0%} — близко к пределу, "
                         "проверьте запас по паспорту")
        ang = float(r["sling_angle_deg"])
        if not (0 < ang <= 90):
            problems.append(f"{lid}: угол ветви {ang}° вне (0, 90] — СТОП")
            stop += 1
        elif ang < 60:
            notes.append(f"{lid}: угол ветви {ang:.0f}° < 60° — несущая способность стропа "
                         "снижается, берите таблицы производителя")
        if int(r["carrying_legs"]) > int(r.get("sling_legs", r["carrying_legs"]) or 0):
            problems.append(f"{lid}: carrying_legs > sling_legs — СТОП")
            stop += 1
        wll = float(r["sling_wll_kg"])
        legs = int(r["carrying_legs"]) or 1
        if wll * legs < m:
            notes.append(f"{lid}: суммарная WLL {wll*legs:.0f} кг < массы {m:.0f} кг — "
                         "проверьте схему строповки")
        ppl = int(r.get("tagline_people", 0) or 0)
        if ppl > max_people:
            problems.append(f"{lid}: людей на оттяжке {ppl} > допуска {max_people} — СТОП")
            stop += 1
        wl = float(r.get("wind_limit_ms", 0) or 0)
        if wl <= 0:
            notes.append(f"{lid}: wind_limit_ms не задан — возьмёт лимит крана; "
                         "осознайте это явно в ППР")

    print(f"Проверено подъёмов: {len(df)}; стоп-замечаний: {stop}; примечаний: {len(notes)}")
    for p in problems:
        print("  ⛔", p)
    for n in notes:
        print("  ⚠ ", n)
    if not stop:
        print("[import_lift] файл пригоден для data/…/lifts.csv (после снятия примечаний "
              "или осознанного принятия их инженером)")
    return 1 if stop else 0


def _interp(radius: float, chart: pd.DataFrame) -> float:
    import numpy as _np
    return float(_np.interp(radius, chart["radius_m"].to_numpy(float),
                            chart["max_load_kg"].to_numpy(float)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["template", "check"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--out", default="data/templates/lift_template.csv")
    ap.add_argument("--crane", default="data/sample/crane_load_chart.csv")
    ap.add_argument("--rules", default="data/sample/rules.yaml")
    a = ap.parse_args()
    if a.mode == "template":
        return template(Path(a.out))
    if not a.file:
        print("check требует файл: python tools/import_lift.py check my_lifts.csv")
        return 2
    return check(Path(a.file), Path(a.crane), Path(a.rules))


if __name__ == "__main__":
    sys.exit(main())
