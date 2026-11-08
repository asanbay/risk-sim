"""
core/sim/sensitivity.py — анализ чувствительности («что если»).

Раздел 11 спецификации требует: менять Cd, массу, угол строп, длину подвеса
на 10-30 % и смотреть, как меняется вывод. Здесь это делается системно:

  * `what_if(...)`      — однократный пересчёт при изменённых входах;
  * `tornado(...)`      — какая неопределённость реально двигает результат;
  * `scenario_grid(...)`— таблица «ветер × оттяжка × Cd» для планёрки.

Смысл торнадо в том, что он показывает, КУДА смотреть при уточнении данных.
Если результат на 70 % определяется Cd, значит нужно заказывать продувку или
считать Cd по справочнику, а не спорить о массе груза до килограмма.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from core.context import Context
from core.io.loaders import Lift, WorkTask
from core.physics import loads as L
from core.physics.pendulum import static_deflection_deg, static_offset_m
from core.physics.wind import profile_factor


@dataclass
class WhatIf:
    label: str
    inputs: dict[str, float]
    wind_force_N: float
    sling_tension_N: float
    sling_utilization: float
    tagline_required_N: float
    tagline_capacity_N: float
    tagline_deficit_N: float
    crane_utilization: float | None
    static_offset_m: float
    theta_deg: float
    ok: bool
    failed: list[str] = field(default_factory=list)


def what_if(ctx: Context, lift: Lift, v_ref_ms: float, *, cd: float | None = None,
            mass_kg: float | None = None, angle_deg: float | None = None,
            carrying_legs: int | None = None, tagline_people: int | None = None,
            daf: float | None = None, radius_m: float | None = None,
            label: str = "") -> WhatIf:
    """Один пересчёт подъёма при изменённых входных величинах."""
    cfg, wind, crane = ctx.cfg, ctx.wind, ctx.ds.crane
    g = cfg.g
    people = cfg.people
    f_person = float(people.get("tagline_force_per_person_N", 200.0))
    eff = float(people.get("tagline_effectiveness", 0.7))

    cd_v = float(cd if cd is not None else lift.cd_mid)
    m = float(mass_kg if mass_kg is not None else lift.mass_kg)
    ang = float(angle_deg if angle_deg is not None else lift.sling_angle_deg)
    legs = int(carrying_legs if carrying_legs is not None else lift.carrying_legs)
    ppl = int(tagline_people if tagline_people is not None else lift.tagline_people)
    d = float(daf if daf is not None else ctx.daf(lift.daf_mode))
    rad = float(radius_m if radius_m is not None else lift.radius_m)

    z = lift.hook_height_m
    v_work = v_ref_ms * profile_factor(z, wind.ref_height_m, wind.alpha)
    F = L.wind_force(v_work, lift.area_m2, cd_v, wind.rho)
    T = L.sling_tension_N(m, legs, ang, g=g, daf=d)
    U_sl = T / (lift.sling_wll_kg * g) if lift.sling_wll_kg > 0 else float("inf")
    cap_kg = crane.capacity_kg(rad)
    U_cr = L.crane_utilization(m, d, cap_kg)
    req = L.tagline_force_required_N(F, float(cfg.param("tagline_force_within_capacity", "margin", 1.0)))
    avail = L.tagline_capacity_N(ppl, f_person, eff)
    deficit = max(0.0, req - avail)
    th = static_deflection_deg(F, m, g)

    failed = []
    if deficit > 0:
        failed.append("tagline_force_within_capacity")
    if U_sl > float(cfg.param("sling_utilization_limit", "max_utilization", 0.8)):
        failed.append("sling_utilization_limit")
    if math.isfinite(U_cr) and U_cr > float(cfg.param("crane_radius_within_chart", "max_utilization", 0.9)):
        failed.append("crane_radius_within_chart")
    if math.isfinite(lift.wind_limit_ms) and v_ref_ms > lift.wind_limit_ms:
        failed.append("stop_crane_ops_above_wind_limit")

    return WhatIf(label=label or f"v={v_ref_ms} м/с",
                  inputs={"v_ref_ms": v_ref_ms, "v_work_ms": round(v_work, 2), "cd": cd_v,
                          "mass_kg": m, "angle_deg": ang, "carrying_legs": legs,
                          "tagline_people": ppl, "daf": d, "radius_m": rad},
                  wind_force_N=round(F, 1), sling_tension_N=round(T, 1),
                  sling_utilization=round(U_sl, 4), tagline_required_N=round(req, 1),
                  tagline_capacity_N=round(avail, 1), tagline_deficit_N=round(deficit, 1),
                  crane_utilization=round(U_cr, 4) if math.isfinite(U_cr) else None,
                  static_offset_m=round(static_offset_m(F, m, lift.suspend_length_m, g), 3),
                  theta_deg=round(th, 3), ok=not failed, failed=failed)


# ---------------------------------------------------------------------------
def tornado(ctx: Context, lift: Lift, v_ref_ms: float, *, pct: float = 20.0) -> list[dict[str, Any]]:
    """Диаграмма-торнадо: как ±pct % по каждому входу меняет целевой показатель.

    Целевой показатель — дефицит усилия на оттяжке (Н), потому что именно он
    чаще всего определяет «можно/нельзя». Возвращает список, отсортированный
    по размаху влияния.
    """
    base = what_if(ctx, lift, v_ref_ms, label="база")
    b = base.tagline_deficit_N

    # (имя, базовое значение, допустимый диапазон, функция пересчёта)
    vars_: list[tuple[str, float, tuple[float, float], Callable[[float], WhatIf]]] = [
        ("Cd (парусность × форма)", lift.cd_mid, (lift.cd_min, lift.cd_max),
         lambda v: what_if(ctx, lift, v_ref_ms, cd=v)),
        ("Масса груза, кг", lift.mass_kg, None,
         lambda v: what_if(ctx, lift, v_ref_ms, mass_kg=v)),
        ("Угол ветви стропа, °", lift.sling_angle_deg, None,
         lambda v: what_if(ctx, lift, v_ref_ms, angle_deg=v)),
        ("Число людей на оттяжке", float(lift.tagline_people), None,
         lambda v: what_if(ctx, lift, v_ref_ms, tagline_people=int(round(v)))),
        ("Скорость ветра на 10 м, м/с", v_ref_ms, None,
         lambda v: what_if(ctx, lift, v)),
        ("Коэф. динамики DAF", ctx.daf(lift.daf_mode), None,
         lambda v: what_if(ctx, lift, v_ref_ms, daf=v)),
    ]

    rows = []
    for name, val, bounds, fn in vars_:
        if name.startswith("Число людей") and val <= 0:
            lo, hi = 0.0, 3.0
        elif bounds is not None:
            lo, hi = bounds
        else:
            lo, hi = val * (1 - pct / 100.0), val * (1 + pct / 100.0)
        if name.startswith("Угол"):
            lo, hi = max(25.0, val - 12.0), min(90.0, val + 12.0)
        w_lo, w_hi = fn(lo), fn(hi)
        rows.append({
            "variable": name, "base_value": round(val, 3),
            "low_value": round(lo, 3), "high_value": round(hi, 3),
            "deficit_at_low": w_lo.tagline_deficit_N, "deficit_at_high": w_hi.tagline_deficit_N,
            "swing": round(abs(w_hi.tagline_deficit_N - w_lo.tagline_deficit_N), 1),
            "offset_low_m": w_lo.static_offset_m, "offset_high_m": w_hi.static_offset_m,
            "ok_low": w_lo.ok, "ok_high": w_hi.ok,
        })
    rows.sort(key=lambda r: -r["swing"])
    return rows


# ---------------------------------------------------------------------------
def scenario_grid(ctx: Context, lift: Lift, *,
                  winds: Sequence[float] = (4, 6, 8, 10, 12, 14, 16, 18),
                  taglines: Sequence[int] = (0, 1, 2, 3, 4),
                  cd: float | None = None) -> pd.DataFrame:
    """Сетка «ветер × число людей на оттяжке» -> дефицит усилия, Н.

    Это таблица для планёрки: по ней видно, при каком ветре и каком числе
    людей подъём ещё удерживается. Красным в отчёте помечается всё, где
    дефицит > 0.
    """
    rows = []
    for v in winds:
        for p in taglines:
            w = what_if(ctx, lift, float(v), cd=cd, tagline_people=int(p))
            rows.append({"v_ref_ms": v, "v_work_ms": w.inputs["v_work_ms"],
                         "tagline_people": p, "wind_force_N": w.wind_force_N,
                         "tagline_capacity_N": w.tagline_required_N - w.tagline_deficit_N,
                         "tagline_deficit_N": w.tagline_deficit_N,
                         "static_offset_m": w.static_offset_m, "theta_deg": w.theta_deg,
                         "ok": w.ok, "failed": ";".join(w.failed)})
    return pd.DataFrame(rows)


def mitigation_options(ctx: Context, lift: Lift, v_ref_ms: float) -> list[dict[str, Any]]:
    """Что нужно изменить, чтобы подъём стал допустимым при данном ветре.

    Возвращает варианты с количественной оценкой — не «добавьте людей», а
    «нужно 4 человека либо уменьшить парусность до 5.2 м²».
    """
    cfg = ctx.cfg
    people = cfg.people
    f_person = float(people.get("tagline_force_per_person_N", 200.0))
    eff = float(people.get("tagline_effectiveness", 0.7))
    margin = float(cfg.param("tagline_force_within_capacity", "margin", 1.0))
    base = what_if(ctx, lift, v_ref_ms)
    need_N = base.tagline_required_N * margin
    out: list[dict[str, Any]] = []

    per_person = f_person * eff
    if per_person > 0:
        n_need = math.ceil(need_N / per_person)
        max_p = int(people.get("max_tagline_people", 4))
        out.append({
            "option": "увеличить число людей на оттяжке",
            "value": f"{n_need} чел. (сейчас {lift.tagline_people})",
            "feasible": 0 < n_need <= max_p,
            "note": (f"в пределах допуска (max {max_p})" if 0 < n_need <= max_p
                     else f"нужно больше {max_p} человек — физически нереализуемо, "
                          f"требуется другое решение"),
        })

    if base.wind_force_N > 0:
        a_need = lift.area_m2 * (base.tagline_capacity_N / (base.wind_force_N * margin)) \
            if base.tagline_capacity_N > 0 else 0.0
        out.append({
            "option": "уменьшить парусность груза",
            "value": f"до {a_need:.1f} м² (сейчас {lift.area_m2:.1f} м²)",
            "feasible": a_need > 0 and lift.tagline_people > 0,
            "note": "делить крупнощитовую опалубку на секции, снимать полотна, "
                    "применять ажурные/сетчатые щиты",
        })

    v_safe = L.max_wind_speed_for_capacity(base.tagline_capacity_N / margin, lift.area_m2,
                                           lift.cd_max, ctx.wind.rho) \
        if base.tagline_capacity_N > 0 else 0.0
    v_safe_ref = v_safe / profile_factor(lift.hook_height_m, ctx.wind.ref_height_m, ctx.wind.alpha)
    out.append({
        "option": "ждать снижения ветра",
        "value": f"до {v_safe_ref:.1f} м/с на 10 м ({v_safe:.1f} м/с на высоте работ)",
        "feasible": v_safe_ref > 1.0,
        "note": "планировать подъём на ночные/утренние окна; см. safe_windows в lift_report",
    })

    out.append({
        "option": "отказаться от удержания людьми",
        "value": "траверса/жёсткое крепление, направляющие тросы, дистанционный захват",
        "feasible": True,
        "note": "единственный надёжный вариант на высотах 200+ м: усилие человека "
                "не масштабируется с парусностью, а она растёт как V²",
    })
    return out
