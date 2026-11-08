"""
core/physics/loads.py — упрощённая физика подъёма груза.

Что считается (каждая формула дублируется в отчёте):

1. Ветровая сила на груз (квазистатика):
       F_w = 0.5 * rho(h) * Cd * A * V(z)^2                                [Н]

2. Усилие в одной несущей ветви стропа (статика, без динамики):
       T_leg = m * g * DAF / (n_carrying * sin(alpha))                       [Н]
   alpha — угол ветви к ГОРИЗОНТАЛИ. n_carrying — сколько ветвей считаем
   несущими (по ППР; при 4 ветвях на жёстком грузе часто считают 2 или 3).
   Дополнительно возвращается геометрический множитель K = 1/sin(alpha)
   и угол между ветвями (included angle) для сверки с таблицами производителя.

3. Использование стропа:
       U_sling = T_leg / (WLL * g)
   WLL задан в кг (как в паспортах). Лимит — из rules.yaml.

4. Оттяжка (tagline). В равновесии сумма горизонтальных сил на грузе равна 0:
       F_w = T_tag * cos(beta) + F_swing_inertia
   Консервативно принимаем beta ~ 0 (оттяжка горизонтальна) и игнорируем
   помощь инерции:
       T_tag_required = F_w
   Доступное усилие людей:
       T_tag_capacity = n_people * f_person * effectiveness
   Если T_tag_required > T_tag_capacity, груз уходит с траектории — нарушение.

5. Грузовая характеристика крана:
       U_crane = m * DAF / Q_chart(R)
   Q_chart(R) — интерполяция таблицы из паспорта.

ВАЖНО: это ПОДДЕРЖКА РЕШЕНИЙ. Допустимые значения (WLL, DAF, лимиты ветра,
усилие человека) берутся из ППР, паспортов и норм, а не подбираются кодом.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pandas as pd

from core.physics.wind import WindField

G_DEFAULT = 9.80665  # м/с2; при необходимости берётся из rules.yaml -> physics.g


# ---------------------------------------------------------------------------
# базовые формулы
# ---------------------------------------------------------------------------
def wind_force(v_ms: float, area_m2: float, cd: float, rho: float = 1.225) -> float:
    """Сила ветра на груз, Н. F = 0.5*rho*Cd*A*V^2."""
    return 0.5 * float(rho) * float(cd) * float(area_m2) * float(v_ms) ** 2


def sling_leg_factor(angle_deg: float) -> float:
    """Геометрический множитель нагрузки ветви K = 1/sin(alpha), alpha — к горизонтали."""
    a = math.radians(float(angle_deg))
    if math.sin(a) <= 1e-6:
        raise ValueError("Угол ветви стропа к горизонтали близок к 0 — схема строповки некорректна.")
    return 1.0 / math.sin(a)


def sling_tension_N(mass_kg: float, carrying_legs: int, angle_deg: float,
                    g: float = 9.80665, daf: float = 1.0) -> float:
    """Усилие в одной несущей ветви стропа, Н.

    T = m*g*DAF / (n * sin(alpha)),  alpha — угол ветви к ГОРИЗОНТАЛИ.
    """
    if carrying_legs <= 0:
        raise ValueError("carrying_legs должен быть >= 1")
    a = math.radians(float(angle_deg))
    if math.sin(a) <= 1e-9:
        # Без этой проверки получается деление на ноль, а при угле 1-2 град —
        # «бесконечная» прочность, которую легко принять за ошибку данных.
        raise ValueError(f"Угол ветви стропа к горизонтали {angle_deg} град недопустим: "
                         f"ветви почти горизонтальны, усилие стремится к бесконечности. "
                         f"Проверьте схему строповки в ППР.")
    return float(mass_kg) * g * float(daf) / (int(carrying_legs) * math.sin(a))


def sling_included_angle_deg(angle_to_horizontal_deg: float, legs: int) -> float:
    """Угол МЕЖДУ ветвями (included angle) — то, что написано в таблицах стропов."""
    if legs <= 1:
        return 0.0
    a = math.radians(float(angle_to_horizontal_deg))
    # для симметричной схемы из n ветвей: полураствор = pi/2 - a  -> included = 2*(pi/2 - a)
    return math.degrees(2.0 * (math.pi / 2.0 - a))


def tagline_force_required_N(wind_force_N: float, margin: float = 1.0) -> float:
    """Требуемое усилие удержания груза оттяжкой, Н (консервативно = ветровой силе)."""
    return float(wind_force_N) * float(margin)


def tagline_capacity_N(n_people: int, force_per_person_N: float, effectiveness: float = 0.7) -> float:
    """Доступное усилие людей на оттяжке, Н."""
    return float(n_people) * float(force_per_person_N) * float(efficiency_safe(effectiveness))


def efficiency_safe(e: float) -> float:
    return float(min(max(e, 0.0), 1.0))


def crane_utilization(mass_kg: float, daf: float, capacity_kg: float) -> float:
    """Использование грузоподъёмности крана на данном вылете."""
    if capacity_kg <= 0:
        return float("inf")
    return float(mass_kg) * float(daf) / float(capacity_kg)


def max_wind_speed_for_capacity(capacity_N: float, area_m2: float, cd: float, rho: float) -> float:
    """Предельная скорость ветра, при которой ветровая сила не превышает capacity_N.

    Обратная формула — нужна, чтобы в отчёте сказать не «опасно», а
    «безопасно до X м/с на высоте работ».
    """
    denom = 0.5 * float(rho) * float(cd) * float(area_m2)
    if denom <= 0:
        return float("inf")
    return math.sqrt(max(0.0, float(capacity_N)) / denom)


# ---------------------------------------------------------------------------
# состояние подъёма
# ---------------------------------------------------------------------------
@dataclass
class LiftState:
    """Мгновенное состояние одного подъёма (одна точка расчёта)."""
    t: Any = None
    v_ref_ms: float = 0.0
    v_work_ms: float = 0.0
    rho: float = 1.225
    cd: float = 1.2
    area_m2: float = 0.0
    mass_kg: float = 0.0
    daf: float = 1.0
    carrying_legs: int = 2
    sling_angle_deg: float = 60.0
    sling_wll_kg: float = 0.0
    tagline_people: int = 0
    tagline_force_per_person_N: float = 200.0
    tagline_effectiveness: float = 0.7
    crane_capacity_kg: float = 0.0
    radius_m: float = 0.0
    wind_limit_ms: float = float("inf")
    height_m: float = 0.0

    # вычисляемые
    wind_force_N: float = 0.0
    sling_tension_N: float = 0.0
    sling_utilization: float = 0.0
    tagline_required_N: float = 0.0
    tagline_capacity_N: float = 0.0
    tagline_deficit_N: float = 0.0
    crane_utilization: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items() if k != "t"}
        d["t"] = str(self.t) if self.t is not None else None
        return d


def evaluate_instant(lift, wind: WindField, t: pd.Timestamp, *, daf: float = 1.0,
                     cd: float | None = None, mass_kg: float | None = None,
                     angle_deg: float | None = None, carrying_legs: int | None = None,
                     tagline_people: int | None = None,
                     crane_capacity_kg: float | None = None,
                     people_cfg: dict | None = None,
                     g: float = G_DEFAULT,
                     z_override: float | None = None) -> LiftState:
    """Полный расчёт одной точки подъёма."""
    cfg = people_cfg or {}
    f_person = float(cfg.get("tagline_force_per_person_N", 200.0))
    eff = float(cfg.get("tagline_effectiveness", 0.7))

    z = float(z_override if z_override is not None else lift.hook_height_m)
    v_ref = wind._series_gust_at(t)                     # измеренный порыв на высоте анемометра
    v_work = wind.gust_at(t, z)                         # порыв на высоте работ (политика из rules.yaml)
    rho = wind.rho

    cd_v = float(cd if cd is not None else lift.cd_mid)
    mass = float(mass_kg if mass_kg is not None else lift.mass_kg)
    ang = float(angle_deg if angle_deg is not None else lift.sling_angle_deg)
    legs = int(carrying_legs if carrying_legs is not None else lift.carrying_legs)
    ppl = int(tagline_people if tagline_people is not None else lift.tagline_people)

    Fw = wind_force(v_work, lift.area_m2, cd_v, rho)
    T = sling_tension_N(mass, legs, ang, g=g, daf=daf)
    U_sling = T / (lift.sling_wll_kg * g) if lift.sling_wll_kg > 0 else float("inf")
    cap_kg = float(crane_capacity_kg if crane_capacity_kg is not None else 0.0)

    req = tagline_force_required_N(Fw)
    avail = tagline_capacity_N(ppl, f_person, eff)

    st = LiftState(
        t=t, v_ref_ms=v_ref, v_work_ms=v_work, rho=rho, cd=cd_v, area_m2=lift.area_m2,
        mass_kg=mass, daf=daf, carrying_legs=legs, sling_angle_deg=ang,
        sling_wll_kg=lift.sling_wll_kg, tagline_people=ppl,
        tagline_force_per_person_N=f_person, tagline_effectiveness=eff,
        crane_capacity_kg=cap_kg, radius_m=lift.radius_m,
        wind_limit_ms=lift.wind_limit_ms, height_m=z,
        wind_force_N=Fw, sling_tension_N=T, sling_utilization=U_sling,
        tagline_required_N=req, tagline_capacity_N=avail, tagline_deficit_N=max(0.0, req - avail),
        crane_utilization=crane_utilization(mass, daf, cap_kg) if cap_kg > 0 else float("nan"),
    )
    return st


def evaluate_lift_series(lift, wind: WindField, *, daf: float = 1.0,
                         crane_capacity_kg: float | None = None,
                         people_cfg: dict | None = None,
                         step_min: float = 15.0,
                         start: pd.Timestamp | None = None,
                         end: pd.Timestamp | None = None,
                         g: float = G_DEFAULT,
                         cd: float | None = None) -> pd.DataFrame:
    """Серия состояний подъёма по времени с шагом step_min (для отчёта и графиков)."""
    t0 = pd.Timestamp(start) if start is not None else None
    t1 = pd.Timestamp(end) if end is not None else None
    if t0 is None or t1 is None:
        raise ValueError("evaluate_lift_series требует start и end (обычно из works по task_id)")
    times = pd.date_range(t0, t1, freq=f"{int(round(step_min))}min")
    rows = []
    for t in times:
        st = evaluate_instant(lift, wind, t, daf=daf, crane_capacity_kg=crane_capacity_kg,
                              people_cfg=people_cfg, cd=cd, g=g)
        d = st.as_dict()
        d["timestamp"] = t.strftime("%Y-%m-%d %H:%M")
        rows.append(d)
    df = pd.DataFrame(rows)
    df.attrs["step_h"] = float(step_min) / 60.0
    return df


def series_summary(df: pd.DataFrame, wind_limit_ms: float) -> dict[str, Any]:
    """Сводка по серии: пики, время превышения, предельно безопасная скорость ветра."""
    if df.empty:
        return {}
    step_h = float(df.attrs.get("step_h", 0.25))
    over = df[df["v_work_ms"] > wind_limit_ms] if np.isfinite(wind_limit_ms) else df.iloc[0:0]
    return {
        "n_points": int(len(df)),
        "step_h": step_h,
        "v_work_max_ms": float(df["v_work_ms"].max()),
        "v_work_mean_ms": float(df["v_work_ms"].mean()),
        "wind_force_max_N": float(df["wind_force_N"].max()),
        "sling_tension_max_N": float(df["sling_tension_N"].max()),
        "sling_utilization_max": float(df["sling_utilization"].max()),
        "tagline_deficit_max_N": float(df["tagline_deficit_N"].max()),
        "tagline_deficit_hours": float((df["tagline_deficit_N"] > 0).sum() * step_h),
        "crane_utilization_max": float(df["crane_utilization"].max()) if df["crane_utilization"].notna().any() else None,
        "wind_limit_exceeded_hours": float(len(over) * step_h),
        "wind_limit_exceeded_pct": float(100.0 * len(over) / len(df)),
    }
