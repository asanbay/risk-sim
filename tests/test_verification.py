"""
tests/test_verification.py — проверка достоверности (раздел 11 спецификации).

Здесь не «тесты кода», а СВЕРКА ФИЗИКИ С ИЗВЕСТНЫМИ СЛУЧАЯМИ. Каждый тест —
это ответ на вопрос «можно ли верить этой цифре». Провалившийся тест означает,
что системе нельзя доверять, а не что «надо поправить ассерт».

Список сверок:
  V1  формула ветровой силы            — ручной расчёт
  V2  усилие в ветви стропа            — ручной расчёт + предельные случаи
  V3  плотность воздуха на высоте      — справочное значение ISA
  V4  период малых колебаний маятника  — T = 2*pi*sqrt(L/g), аналитика vs MuJoCo
  V5  статическое отклонение на ветру  — tan(theta) = F/(m*g), аналитика vs MuJoCo
  V6  усилие в подвесе в статике       — T = sqrt((mg)^2 + F^2)
  V7  сходимость двух движков          — analytic vs MuJoCo в пределах допуска
  V8  степенной профиль ветра          — монотонность и контрольная точка
  V9  Monte Carlo воспроизводим        — один seed -> один результат
  V10 conservation: без ветра маятник  — амплитуда не растёт
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.physics import loads as L
from core.physics.pendulum import (pendulum_period, static_deflection_deg, static_offset_m,
                                   simulate_pendulum, suspension_tension_N)
from core.physics.wind import air_density, profile_factor, turbulence_intensity, gust_factor, WindField

G = 9.80665


# ---------------------------------------------------------------------------
# V1  ветровая сила: ручной расчёт
# ---------------------------------------------------------------------------
def test_V1_wind_force_hand_calc():
    """0.5 * 1.225 * 1.2 * 18 * 12^2 = 1905.1 Н  (пример из раздела 8.2)."""
    F = L.wind_force(12.0, 18.0, 1.2, rho=1.225)
    assert abs(F - 1905.12) < 0.5, F


def test_V1b_wind_force_scales_with_v_squared():
    """Удвоение скорости -> учетверение силы. Это то, что чаще всего ломают."""
    f1 = L.wind_force(10, 12, 1.3, 1.177)
    f2 = L.wind_force(20, 12, 1.3, 1.177)
    assert abs(f2 / f1 - 4.0) < 1e-9


def test_V1c_wind_force_zero_at_zero_wind():
    assert L.wind_force(0.0, 18.0, 1.2) == 0.0


# ---------------------------------------------------------------------------
# V2  усилие в ветви стропа
# ---------------------------------------------------------------------------
def test_V2_sling_tension_hand_calc():
    """4000 кг, 2 несущие ветви, 60 град к горизонтали:
       T = 4000*9.80665 / (2*sin60) = 22646.6 Н  (2308 кгс на ветвь).
    """
    T = L.sling_tension_N(4000, 2, 60.0)
    expected = 4000 * G / (2 * math.sin(math.radians(60)))
    assert abs(T - expected) < 1e-6
    assert abs(T - 22647.2) < 0.5, T


def test_V2b_sling_vertical_limit():
    """При 90 град (ветви вертикальны) множитель = 1: T = m*g/n."""
    T = L.sling_tension_N(1000, 4, 90.0)
    assert abs(T - 1000 * G / 4) < 1e-6


def test_V2c_sling_angle_increases_tension():
    """Чем положе ветвь, тем больше усилие — монотонность обязательна."""
    ts = [L.sling_tension_N(2000, 2, a) for a in (80, 70, 60, 50, 40, 30)]
    assert all(b > a for a, b in zip(ts, ts[1:]))


def test_V2d_included_angle():
    """4 ветви при 60 град к горизонтали -> угол между ветвями 60 град.
    Это то число, которое смотрят в таблице производителя стропа."""
    assert abs(L.sling_included_angle_deg(60.0, 4) - 60.0) < 1e-9
    assert abs(L.sling_included_angle_deg(45.0, 4) - 90.0) < 1e-9
    assert L.sling_included_angle_deg(60.0, 1) == 0.0


def test_V2e_leg_factor():
    assert abs(L.sling_leg_factor(30.0) - 2.0) < 1e-9   # 1/sin30 = 2
    assert abs(L.sling_leg_factor(90.0) - 1.0) < 1e-9


def test_V2f_sling_rejects_degenerate_angle():
    with pytest.raises(ValueError):
        L.sling_tension_N(1000, 2, 0.0)


# ---------------------------------------------------------------------------
# V3  плотность воздуха
# ---------------------------------------------------------------------------
def test_V3_air_density_altitude():
    """ISA: на 400 м rho ≈ 1.177 кг/м3 (справочное значение).
    На уровне моря — ровно 1.225."""
    assert abs(air_density(0.0) - 1.225) < 1e-9
    assert abs(air_density(400.0) - 1.177) < 0.003
    assert air_density(400.0) < air_density(0.0)
    assert air_density(400.0, model="constant") == 1.225


# ---------------------------------------------------------------------------
# V4  период маятника
# ---------------------------------------------------------------------------
def test_V4_pendulum_period_formula():
    """L = 12 м -> T = 2*pi*sqrt(12/9.80665) = 6.946 с."""
    T = pendulum_period(12.0)
    assert abs(T - 2 * math.pi * math.sqrt(12 / G)) < 1e-9
    assert abs(T - 6.946) < 0.01


def test_V4b_analytic_sim_reproduces_period():
    """Аналитический движок: измеренный период совпадает с формулой."""
    res = simulate_pendulum(lambda t: 0.0, duration_s=60.0, mass_kg=1800.0,
                            length_m=12.0, area_m2=0.0, cd=1.2,
                            timestep=0.002, sample_every_s=0.02,
                            initial_theta_deg=3.0, engine="analytic")
    T_meas = res.measured_period_s()
    T_th = pendulum_period(12.0)
    assert T_meas is not None
    assert abs(T_meas - T_th) / T_th < 0.01, (T_meas, T_th)


@pytest.mark.skipif(os.environ.get("SKIP_MUJOCO") == "1", reason="MuJoCo отключён")
def test_V4c_mujoco_reproduces_period():
    """MuJoCo: тот же маятник, тот же период. Это главная сверка движков."""
    pytest.importorskip("mujoco")
    res = simulate_pendulum(lambda t: 0.0, duration_s=60.0, mass_kg=1800.0,
                            length_m=12.0, area_m2=0.0, cd=1.2, anchor=(0, 0, 30),
                            timestep=0.002, sample_every_s=0.05,
                            initial_theta_deg=3.0, engine="mujoco")
    T_meas = res.measured_period_s()
    T_th = pendulum_period(12.0)
    assert T_meas is not None
    assert abs(T_meas - T_th) / T_th < 0.02, (T_meas, T_th)


# ---------------------------------------------------------------------------
# V5  статическое отклонение
# ---------------------------------------------------------------------------
def test_V5_static_deflection_hand_calc():
    """F = 3782 Н, m = 1800 кг -> tan(theta) = 3782/(1800*9.80665) = 0.2143
       -> theta = 12.10 град."""
    th = static_deflection_deg(3782.0, 1800.0)
    assert abs(th - math.degrees(math.atan(3782.0 / (1800.0 * G)))) < 1e-9
    assert abs(th - 12.10) < 0.02, th


def test_V5b_static_deflection_zero_wind():
    assert static_deflection_deg(0.0, 1000.0) == 0.0
    assert static_offset_m(0.0, 1000.0, 12.0) == 0.0


def test_V5c_offset_geometry():
    """Смещение = L*sin(theta) и всегда меньше длины подвеса."""
    off = static_offset_m(5000.0, 1800.0, 12.0)
    th = math.radians(static_deflection_deg(5000.0, 1800.0))
    assert abs(off - 12.0 * math.sin(th)) < 1e-9
    assert 0 < off < 12.0


@pytest.mark.skipif(os.environ.get("SKIP_MUJOCO") == "1", reason="MuJoCo отключён")
def test_V5d_mujoco_static_deflection():
    """MuJoCo: постоянный ветер -> установившееся отклонение = формуле.
    Двигатель с аэродинамикой по относительной скорости обязан выйти на
    tan(theta) = F/(m*g), когда груз перестал двигаться.
    """
    pytest.importorskip("mujoco")
    m, A, cd, rho, V = 1800.0, 12.0, 1.2, 1.225, 15.0
    F = L.wind_force(V, A, cd, rho)
    res = simulate_pendulum(lambda t: V, duration_s=180.0, mass_kg=m, length_m=14.0,
                            area_m2=A, cd=cd, rho=rho, anchor=(0, 0, 280),
                            timestep=0.002, sample_every_s=0.2, engine="mujoco")
    th_meas = float(np.median(res.theta_deg[len(res.theta_deg) // 2:]))
    th_th = static_deflection_deg(F, m)
    assert abs(th_meas - th_th) < max(0.6, 0.06 * th_th), (th_meas, th_th)


# ---------------------------------------------------------------------------
# V6  усилие в подвесе
# ---------------------------------------------------------------------------
def test_V6_suspension_tension_static():
    """В статике (omega = 0): T = m*g*cos(th) + F*sin(th) = sqrt((mg)^2 + F^2)."""
    m, F = 1800.0, 3782.0
    th = static_deflection_deg(F, m)
    T = suspension_tension_N(m, th, 0.0, 12.0, wind_force_N=F)
    assert abs(T - math.hypot(m * G, F)) < 1e-6


def test_V6b_suspension_tension_no_wind():
    """Без ветра и без движения T = m*g (вес)."""
    T = suspension_tension_N(1000.0, 0.0, 0.0, 12.0, wind_force_N=0.0)
    assert abs(T - 1000 * G) < 1e-9


def test_V6c_dynamic_adds_to_tension():
    """Раскачка увеличивает усилие в подвесе — монотонность по omega."""
    ts = [suspension_tension_N(1800, 10.0, w, 12.0, wind_force_N=2000) for w in (0, 0.2, 0.5)]
    assert ts[0] < ts[1] < ts[2]


# ---------------------------------------------------------------------------
# V7  сходимость двух движков
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.environ.get("SKIP_MUJOCO") == "1", reason="MuJoCo отключён")
def test_V7_analytic_vs_mujoco():
    """Один и тот же физический случай, два движка. Расхождение < 8 %
    по максимальному углу — иначе один из них врёт."""
    pytest.importorskip("mujoco")
    wf = lambda t: 12.0 + 5.0 * math.sin(0.35 * t)
    kw = dict(duration_s=90.0, mass_kg=1800.0, length_m=14.0, area_m2=12.0,
              cd=1.2, rho=1.225, anchor=(0, 0, 280), initial_theta_deg=1.0)
    a = simulate_pendulum(wf, timestep=0.001, sample_every_s=0.1, engine="analytic", **kw)
    b = simulate_pendulum(wf, timestep=0.001, sample_every_s=0.1, engine="mujoco", **kw)

    # Сравниваем УСТАНОВИВШУЮСЯ часть (вторая половина записи): первые
    # секунды — переходный процесс от начального отклонения, он чувствителен
    # к фазе и не является проверкой физики.
    n = len(a.t) // 2
    sa, sb = a.theta_deg[n:], b.theta_deg[n:]
    stats = {}
    for name, va, vb in (("rms", np.sqrt(np.mean(sa**2)), np.sqrt(np.mean(sb**2))),
                         ("max", np.max(np.abs(sa)), np.max(np.abs(sb))),
                         ("mean", np.mean(sa), np.mean(sb))):
        rel = abs(va - vb) / max(abs(vb), 1e-6)
        stats[name] = (round(float(va), 4), round(float(vb), 4), round(float(rel), 4))
        assert rel < 0.03, (name, va, vb, rel)

    rel_off = abs(a.steady_offset_m - b.steady_offset_m) / max(b.steady_offset_m, 1e-6)
    assert rel_off < 0.05, (a.steady_offset_m, b.steady_offset_m, rel_off)

    # период отклика обязан совпадать (это самая жёсткая проверка интегратора)
    Ta, Tb = a.measured_period_s(), b.measured_period_s()
    assert abs(Ta - Tb) / Tb < 0.01, (Ta, Tb)


# ---------------------------------------------------------------------------
# V8  профиль ветра
# ---------------------------------------------------------------------------
def test_V8_profile_monotonic_and_exact():
    assert abs(profile_factor(10.0, 10.0, 0.16) - 1.0) < 1e-12
    assert profile_factor(250, 10, 0.16) > profile_factor(100, 10, 0.16) > 1.0
    # контрольная точка: (250/10)^0.16 = 1.6964
    assert abs(profile_factor(250, 10, 0.16) - 1.67371) < 1e-4   # 25^0.16


def test_V8b_turbulence_decreases_with_height():
    """Iu обязана убывать с высотой — иначе модель порывов бессмысленна."""
    iu = [turbulence_intensity(z, 0.05) for z in (10, 50, 150, 300)]
    assert all(b < a for a, b in zip(iu, iu[1:]))
    assert 0.10 < iu[0] < 0.30, iu[0]     # типовое значение на 10 м
    assert iu[-1] < 0.16, iu[-1]          # на 300 м заметно меньше


def test_V8c_gust_factor_reasonable():
    """G(3 с / 1 ч) на 10 м для открытой местности: 1.4-1.8; с высотой убывает."""
    g10 = gust_factor(10.0, 3600, 3, 0.05)
    g250 = gust_factor(250.0, 3600, 3, 0.05)
    assert 1.35 < g10 < 1.85, g10
    assert g250 < g10, (g250, g10)
    assert gust_factor(10.0, 3600, 600, 0.05) < g10   # более длинное осреднение -> меньше порыв


def test_V8d_wind_field_interpolation_is_time_correct():
    """Регрессия: pandas может вернуть datetime64[us], и наивное деление
    asi8/1e9 ломает интерполяцию — все запросы «залипают» на последнем
    значении. Тест ловит именно это."""
    ts = pd.date_range("2026-11-16 00:00", periods=5, freq="h")
    wf = WindField(ts=ts, mean_ms=np.array([2.0, 4.0, 8.0, 16.0, 4.0]),
                   gust_ms=np.array([3.0, 6.0, 12.0, 24.0, 6.0]),
                   ref_height_m=10.0, alpha=0.0, gust_policy="measured")
    assert abs(wf.mean_at(pd.Timestamp("2026-11-16 00:00"), 10) - 2.0) < 1e-9
    assert abs(wf.mean_at(pd.Timestamp("2026-11-16 02:00"), 10) - 8.0) < 1e-9
    assert abs(wf.mean_at(pd.Timestamp("2026-11-16 02:30"), 10) - 12.0) < 1e-9  # интерполяция 8->16
    assert abs(wf.mean_at(pd.Timestamp("2026-11-16 03:30"), 10) - 10.0) < 1e-9  # интерполяция 16->4
    assert abs(wf.mean_at(pd.Timestamp("2026-11-16 04:00"), 10) - 4.0) < 1e-9
    assert wf.covers(pd.Timestamp("2026-11-16 02:00"))
    assert not wf.covers(pd.Timestamp("2026-11-20 02:00"))


# ---------------------------------------------------------------------------
# V9  воспроизводимость Monte Carlo
# ---------------------------------------------------------------------------
def test_V9_monte_carlo_reproducible():
    from core.context import build_context
    from core.sim.montecarlo import lift_monte_carlo
    ctx = build_context("data/sample")
    lf = ctx.ds.lifts[1]
    task = ctx.ds.task_by_id[lf.task_id]
    a = lift_monte_carlo(ctx, lf, task, n=800, seed=42)
    b = lift_monte_carlo(ctx, lf, task, n=800, seed=42)
    c = lift_monte_carlo(ctx, lf, task, n=800, seed=43)
    assert a.probabilities == b.probabilities
    assert a.probabilities != c.probabilities or a.n < 50   # разные seed -> разные числа
    for v in a.probabilities.values():
        assert 0.0 <= v <= 1.0


def test_V9b_mc_probabilities_are_frequencies():
    """P обязана быть частотой: при заведомо невозможном отказе -> 0."""
    from core.context import build_context
    from core.sim.montecarlo import lift_monte_carlo
    ctx = build_context("data/sample")
    lf = ctx.ds.lifts[2]                      # пучок арматуры: малая парусность
    task = ctx.ds.task_by_id[lf.task_id]
    r = lift_monte_carlo(ctx, lf, task, n=3000, seed=1)
    assert r.probabilities["P_sling_utilization_exceeded"] == 0.0   # WLL с большим запасом
    assert 0.0 <= r.probabilities["P_tagline_force_exceeded"] <= 1.0


# ---------------------------------------------------------------------------
# V10  сохранение энергии / устойчивость
# ---------------------------------------------------------------------------
def test_V10_no_wind_amplitude_does_not_grow():
    """Без ветра и без демпфирования амплитуда не должна РАСТИ (численная
    неустойчивость — типичная причина «кран качается сам по себе»)."""
    res = simulate_pendulum(lambda t: 0.0, duration_s=120.0, mass_kg=2000.0,
                            length_m=14.0, area_m2=0.0, cd=0.0,
                            timestep=0.002, sample_every_s=0.05,
                            initial_theta_deg=5.0, engine="analytic")
    th = res.theta_deg
    first = float(np.max(th[:200]))
    last = float(np.max(th[-200:]))
    assert last <= first * 1.02, (first, last)


def test_V10b_tagline_reduces_offset():
    """Оттяжка обязана уменьшать смещение груза — иначе она не работает."""
    wf = lambda t: 14.0
    kw = dict(duration_s=150.0, mass_kg=1800.0, length_m=14.0, area_m2=12.0,
              cd=1.2, rho=1.225, anchor=(0, 0, 280), engine="analytic",
              timestep=0.005, sample_every_s=0.1)
    free = simulate_pendulum(wf, tagline_capacity_N=0.0, **kw)
    held = simulate_pendulum(wf, tagline_capacity_N=1200.0, **kw)
    assert held.steady_offset_m < free.steady_offset_m, (held.steady_offset_m, free.steady_offset_m)


def test_V10c_heavier_load_deflects_less():
    """При той же парусности тяжёлый груз отклоняется меньше."""
    th_light = static_deflection_deg(3000.0, 800.0)
    th_heavy = static_deflection_deg(3000.0, 4000.0)
    assert th_light > th_heavy


# ---------------------------------------------------------------------------
# V11  сквозная проверка на данных проекта
# ---------------------------------------------------------------------------
def test_V11_pipeline_end_to_end():
    """Полный прогон ядра: данные -> физика -> правила -> находки.
    Гарантирует, что модули согласованы и ничего не отвалилось молча."""
    from core.context import build_context
    from core.sim.rules import analyze
    ctx = build_context("data/sample")
    la = ctx.build_lift_assessments(run_swing=False)
    eng, findings, df = analyze(ctx.ds, ctx.cfg, ctx.wind, la)
    assert len(findings) > 0, "на демо-данных должны находиться конфликты"
    # каждое нарушение обязано нести ссылку на ППР/норму
    assert all(f.source and len(f.source) > 5 for f in findings)
    # severity только из разрешённого набора
    assert set(df["severity"]) <= {"critical", "high", "medium", "low"}
    # у каждого нарушения есть интервал или координата
    assert all((f.start is not None) or (f.location is not None) for f in findings)


def test_V11b_data_validation_rejects_bad_input(tmp_path):
    """Загрузчик обязан ПАДАТЬ на битых данных, а не подставлять значения по
    умолчанию: молчаливое допущение в задачах безопасности дороже ошибки."""
    from core.io.loaders import load_zones
    # пропущенная guardrail_installed -> НЕ True (консервативно: ограждения нет)
    soft = tmp_path / "zones_soft.csv"
    soft.write_text("zone_id,level,type,x_min,y_min,x_max,y_max,z_floor,z_top\n"
                    "Z1,L1,work,0,0,1,1,0,3\n", encoding="utf-8")
    zz = load_zones(soft)
    assert zz[0].guardrail_installed is False, "отсутствующее ограждение не должно читаться как установленное"
    # обязательные колонки отсутствуют -> ошибка
    bad = tmp_path / "zones.csv"
    bad.write_text("zone_id,level,x_min,y_min\nZ1,L1,0,0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_zones(bad)
    bad2 = tmp_path / "zones2.csv"
    bad2.write_text("zone_id,level,type,x_min,y_min,x_max,y_max,z_floor,z_top\n"
                    "Z1,L1,unknown_type,0,0,1,1,0,3\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_zones(bad2)


def test_V11c_rules_require_source(tmp_path):
    """Правило без ссылки на ППР/норму не должно загружаться."""
    from core.config import load_rules
    p = tmp_path / "rules.yaml"
    p.write_text("meta: {}\nphysics: {}\npeople: {}\nrules:\n  - id: r1\n    title: t\n    severity: high\n",
                 encoding="utf-8")
    with pytest.raises(ValueError):
        load_rules(p)
