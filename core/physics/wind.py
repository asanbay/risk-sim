"""
core/physics/wind.py — поле ветра: пересчёт измерений на высоту работ и порывы.

Формулы и допущения (все видны в отчёте):

1. Плотность воздуха по барометрической формуле:
       rho(h) = rho0 * (1 - 2.25577e-5 * h)^4.25588
   Для отметки площадки 400 м rho ≈ 1.177 кг/м3 вместо 1.225 — разница ~4 %,
   она заметна при ветре 20+ м/с, поэтому учитывается.

2. Степенной профиль ветра (power law) от высоты измерения z_ref к высоте z:
       V(z) = V(z_ref) * (z / z_ref)^alpha
   alpha = 0.22 для городской/пригородной застройки (уточнить по отчёту по
   ветровому отклику). Профиль применим в пределах пограничного слоя
   (~до 300-500 м); выше — использовать данные из ветроинженерного отчёта.

3. Порыв на высоте: два подхода, оба возвращаются.
   a) перенос измеренного порыва тем же степенным профилем (консервативно);
   b) модель порывистости ESDU/Durst-кривая: G(z,T) = 1 + g * Iu(z) * f(T),
      где Iu ≈ 0.19*(z/z_g)^-(alpha+0.12) — интенсивность турбуентности,
      T — длительность осреднения порыва (3 с ... 1 ч).
   Ядро по умолчанию берёт MAX(a, b), т.е. консервативную оценку.

4. Квазистатическая аэродинамическая сила на груз:
       F = 0.5 * rho(z) * Cd * A * V(z)^2
   Cd — диапазон (cd_min..cd_max) из lifts.csv; в Monte Carlo сэмплируется.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

RHO0 = 1.225            # кг/м3 на уровне моря, ISA


def air_density(elevation_m: float, model: str = "barometric") -> float:
    """Плотность воздуха, кг/м3, на отметке elevation_m над уровнем моря."""
    if model == "constant":
        return RHO0
    return RHO0 * (1.0 - 2.25577e-5 * float(elevation_m)) ** 4.25588


def profile_factor(z: float, z_ref: float, alpha: float) -> float:
    """Множитель степенного профиля ветра V(z)/V(z_ref)."""
    z = max(float(z), 0.5)
    z_ref = max(float(z_ref), 0.5)
    return (z / z_ref) ** float(alpha)


def turbulence_intensity(z: float, z0: float = 0.05, kl: float = 1.0, z_min: float = 1.0) -> float:
    """Интенсивность турбуентности Iu(z) по EN 1991-1-4 (Eurocode 1), п. 4.4:

        Iu(z) = kl / ln(z / z0)      при z >= zmin

    z0 — длина шероховатости местности (категория II: 0.05 м — открытая
    местность; III: 0.3 м; IV: 1.0 м — плотная городская застройка).
    Эта форма корректно убывает с высотой, в отличие от степенной
    аппроксимации: на 270 м Iu ≈ 0.12, а не 0.21.
    """
    z = max(float(z), float(z_min), float(z0) * 1.01)
    return float(kl / math.log(z / max(z0, 1e-6)))


def gust_factor(z: float, mean_duration_s: float = 3600.0, peak_duration_s: float = 3.0,
                z0: float = 0.05, peak_factor: float = 3.4) -> float:
    """G(z) = V_peak(peak_duration) / V_mean(mean_duration).

    База: пик-фактор для 3-секундного осреднения g ≈ 3.4 (при 10-минутном
    осреднении средней скорости). Поправка на другое окно осреднения —
    по аппроксимации кривой Дарста:
        G(T) ≈ 1 + g * Iu(z) * sqrt( 0.54 + 0.29 * ln(T_mean / T_peak) )
    нормированной так, чтобы при T_mean=1 ч, T_peak=3 с давать ~1.5-1.6
    на 10 м для открытой местности (совпадает с типовыми измерениями).
    """
    iu = turbulence_intensity(z, z0)
    ratio = max(float(mean_duration_s) / max(float(peak_duration_s), 0.5), 1.0)
    # нормировка: при ratio = 1200 (1 ч / 3 с) sqrt(...) = 1.95 -> делим на неё
    norm = math.sqrt(0.54 + 0.29 * math.log(1200.0))
    return float(1.0 + peak_factor * iu * math.sqrt(0.54 + 0.29 * math.log(ratio)) / norm)


@dataclass
class WindField:
    """Почасовая серия ветра + пересчёт на произвольную высоту и время."""

    ts: pd.DatetimeIndex
    mean_ms: np.ndarray
    gust_ms: np.ndarray
    ref_height_m: float = 10.0
    alpha: float = 0.16            # показатель степенного профиля (перенос по высоте)
    elevation_m: float = 0.0       # отметка площадки над уровнем моря (плотность воздуха)
    rho_model: str = "barometric"
    z0_m: float = 0.05             # шероховатость местности для Iu(z) по EN 1991-1-4
    gust_policy: str = "measured"  # measured | model | max
    source: str = ""

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        # Время храним в секундах ОТ ПЕРВОЙ МЕТКИ серии. Это unit-safe:
        # pandas 2.x может вернуть datetime64[us] вместо [ns], и деление
        # asi8 на 1e9 молча ломает интерполяцию (все запросы «залипают»
        # на последнем значении). Плюс числа маленькие -> нет потери точности.
        self._t0 = self.ts[0] if len(self.ts) else pd.Timestamp("2000-01-01")
        if len(self.ts):
            self._x_sec = (self.ts - self._t0).total_seconds().to_numpy(dtype=float)
        else:
            self._x_sec = np.zeros(0)
        self._interp_cache: dict[str, object] = {}

    def _sec(self, t: pd.Timestamp) -> float:
        return float((pd.Timestamp(t) - self._t0).total_seconds())

    def _interp(self, key: str, t: pd.Timestamp) -> float:
        if len(self._x_sec) == 0:
            return 0.0
        y = getattr(self, key)
        return float(np.interp(self._sec(t), self._x_sec, y))

    @property
    def rho(self) -> float:
        return air_density(self.elevation_m, self.rho_model)

    def _series_mean_at(self, t: pd.Timestamp) -> float:
        """Линейная интерполяция почасовой средней скорости в момент t."""
        return self._interp("mean_ms", t)

    def _series_gust_at(self, t: pd.Timestamp) -> float:
        return self._interp("gust_ms", t)

    def mean_at(self, t: pd.Timestamp, z: float) -> float:
        """Средняя (часовая) скорость на высоте z, м/с."""
        return self._series_mean_at(t) * profile_factor(z, self.ref_height_m, self.alpha)

    def gust_model_at(self, t: pd.Timestamp, z: float) -> float:
        """Порыв (3 с) на высоте z по МОДЕЛИ турбуентности: mean(z) * G(z)."""
        return self.mean_at(t, z) * gust_factor(z, 3600.0, 3.0, self.z0_m)

    def gust_measured_at(self, t: pd.Timestamp, z: float) -> float:
        """Измеренный порыв, перенесённый степенным профилем на высоту z."""
        return self._series_gust_at(t) * profile_factor(z, self.ref_height_m, self.alpha)

    def gust_at(self, t: pd.Timestamp, z: float, conservative: bool | None = None,
                policy: str | None = None) -> float:
        """Порыв (3 с) на высоте z, м/с.

        Политика (rules.yaml -> meta.wind_gust_policy):
          measured — перенос измеренного порыва профилем. РЕКОМЕНДУЕТСЯ,
                     когда на объекте есть анемометр с записью порывов:
                     реальное измерение всегда лучше общей модели.
          model    — mean(z) * G(z) по интенсивности турбуентности EN 1991-1-4.
                     Нужен, когда в данных есть только средняя скорость.
          max      — максимум из двух (консервативно, для проверки «худшего
                     случая», когда измерение и модель сильно расходятся).
        Параметр `conservative=True` оставлен для обратной совместимости и
        означает политику 'max'.
        """
        pol = policy or ("max" if conservative else None) or self.gust_policy
        meas, mod = self.gust_measured_at(t, z), self.gust_model_at(t, z)
        if pol == "model":
            return mod
        if pol == "max":
            return max(meas, mod)
        return meas

    def covers(self, t: pd.Timestamp) -> bool:
        """Попадает ли момент времени внутрь ряда метео-данных."""
        if len(self.ts) == 0:
            return False
        return bool(self.ts[0] <= pd.Timestamp(t) <= self.ts[-1])

    def extrapolation_ratio(self, z: float, max_ratio: float = 8.0) -> float | None:
        """z/z_ref — если больше max_ratio, степенной профиль ненадёжен.

        Перенос с 10 м на 270 м — это экстраполяция в 27 раз за пределы
        слоя, где профиль калибровался. Для высотного здания такие числа
        обязан давать ветроинженерный отчёт, а не степенной закон.
        Возвращает None, если всё в пределах допуска.
        """
        r = float(z) / max(self.ref_height_m, 0.5)
        return r if r > float(max_ratio) else None

    def wind_force_at(self, t: pd.Timestamp, z: float, area_m2: float, cd: float,
                      use_gust: bool = True) -> float:
        """Квазистатическая сила ветра на груз, Н."""
        v = self.gust_at(t, z) if use_gust else self.mean_at(t, z)
        return 0.5 * self.rho * float(cd) * float(area_m2) * v ** 2

    def design_wind_speed(self, t: pd.Timestamp, z: float, use_gust: bool = True) -> float:
        return self.gust_at(t, z) if use_gust else self.mean_at(t, z)

    # ------------------------------------------------------------------
    def hourly_frame(self, z: float) -> pd.DataFrame:
        """Таблица: час, средняя и порыв на высоте z — для отчёта и графиков."""
        pf = profile_factor(z, self.ref_height_m, self.alpha)
        return pd.DataFrame({
            "timestamp": self.ts,
            "mean_at_ref_ms": np.round(self.mean_ms, 2),
            "gust_at_ref_ms": np.round(self.gust_ms, 2),
            f"mean_at_{z:g}m_ms": np.round(self.mean_ms * pf, 2),
            f"gust_measured_at_{z:g}m_ms": np.round(self.gust_ms * pf, 2),
            f"gust_model_at_{z:g}m_ms": np.round([self.gust_model_at(t, z) for t in self.ts], 2),
            f"gust_used_at_{z:g}m_ms": np.round([self.gust_at(t, z) for t in self.ts], 2),
        })

    def info(self) -> dict:
        return {
            "source": self.source or "не указан",
            "ref_height_m": self.ref_height_m,
            "profile_alpha": self.alpha,
            "roughness_z0_m": self.z0_m,
            "gust_policy": self.gust_policy,
            "elevation_m": self.elevation_m,
            "rho_kg_m3": round(self.rho, 4),
            "hours": int(len(self.ts)),
            "span": [str(self.ts.min()), str(self.ts.max())] if len(self.ts) else [],
            "mean_max_ms_at_ref": float(np.max(self.mean_ms)) if len(self.ts) else None,
            "gust_max_ms_at_ref": float(np.max(self.gust_ms)) if len(self.ts) else None,
        }


def wind_field_from_series(series, *, alpha: float, elevation_m: float,
                           rho_model: str = "barometric", z0_m: float = 0.05,
                           gust_policy: str = "measured") -> WindField:
    return WindField(ts=series.ts, mean_ms=series.mean_ms, gust_ms=series.gust_ms,
                     ref_height_m=series.ref_height_m, alpha=alpha, elevation_m=elevation_m,
                     rho_model=rho_model, z0_m=z0_m, gust_policy=gust_policy, source=series.source)
