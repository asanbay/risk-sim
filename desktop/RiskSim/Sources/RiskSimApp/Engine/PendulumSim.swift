import Foundation
import simd

/// Живой интегратор маятника с подвижной точкой подвеса и ветром.
///
/// Это сердце живого режима движка: пока кран движется по программной
/// траектории (кадры расчёта), груз качается по уравнению маятника,
/// проинтегрированному ПРЯМО В ДВИЖКЕ на фиксированном шаге 120 Гц:
///
///   θ'' = -(g/L)·sin θ − (a_pivot/L)·cos θ − (2L'/L)·θ' − c·θ' + F_ветра·cos θ/(m·L)
///
/// где a_pivot — горизонтальное ускорение тележки/крюка (конечные разности
/// программной траектории), F_ветра = ½·ρ·Cd·A·v². Режим «Повтор» играет
/// MuJoCo-кадры; живой режим считает сам — расхождение ловит валидатор.
struct PendulumState {
    var theta: Double = 0        // угол от вертикали, рад (в плоскости стрелы)
    var omega: Double = 0        // угловая скорость, рад/с
    var length: Double = 10      // длина подвеса, м
}

final class LivePendulumSim {
    var state = PendulumState()
    var g = 9.81
    var damping = 0.015              // 1/с — аэродинамическое затухание
    static let fixedH = 1.0 / 120.0  // фиксированный шаг интегрирования

    /// Один шаг полу-неявным Эйлером. aPivot — ускорение подвеса вдоль
    /// плоскости стрелы (м/с²), windForceN — сила ветра на груз (Н, вдоль
    /// той же плоскости), massKg — масса груза.
    func step(h: Double, aPivot: Double, length: Double,
              windForceN: Double, massKg: Double) {
        let L = max(1.0, length)
        state.length = L
        let m = max(1.0, massKg)
        let alpha = -(g / L) * sin(state.theta)
            - (aPivot / L) * cos(state.theta)
            - (damping) * state.omega
            + (windForceN * cos(state.theta)) / (m * L)
        state.omega += alpha * h
        state.theta = clampTheta(state.theta + state.omega * h)
    }

    /// Полшага-назад для устойчивости при резких переходах (клэмп ±87°).
    private func clampTheta(_ t: Double) -> Double {
        var t = t
        let limit = 87.0 * .pi / 180.0
        if t > limit { t = limit; state.omega = min(0, state.omega) }
        if t < -limit { t = -limit; state.omega = max(0, state.omega) }
        return t
    }

    /// Сила ветра на груз, Н (квадратичный напор).
    static func windForce(vMs: Double, areaM2: Double, cd: Double = 1.2,
                          rho: Double = 1.2) -> Double {
        0.5 * rho * cd * max(0.2, areaM2) * vMs * vMs
    }
}

/// Интерполятор программной траектории крана по кадрам MuJoCo-расчёта.
/// Живой режим движет краном по этой траектории, а груз — своим интегратором.
struct CranePathSampler {
    let frames: [TimelineFrame]
    let dtSeconds: Double

    init(frames: [TimelineFrame], dtMinutes: Double) {
        self.frames = frames
        self.dtSeconds = dtMinutes * 60.0
    }

    var durationSeconds: Double { Double(max(0, frames.count - 1)) * dtSeconds }

    struct Sample {
        var slewDeg: Double
        var radiusM: Double
        var hookZM: Double
        var windTopMs: Double
        var windRefMs: Double
    }

    func sample(at t: Double) -> Sample {
        let x = max(0, min(t, durationSeconds))
        let f = x / dtSeconds
        let i = min(Int(f), frames.count - 2)
        let k = f - Double(i)
        let a = frames[i], b = frames[i + 1]
        func lerp(_ va: Double?, _ vb: Double?) -> Double {
            switch (va, vb) {
            case let (va?, vb?): return va + (vb - va) * k
            case let (va?, nil): return va
            case let (nil, vb?): return vb
            default: return 0
            }
        }
        return Sample(
            slewDeg: lerp(a.crane?.slewDeg, b.crane?.slewDeg),
            radiusM: lerp(a.crane?.radiusM, b.crane?.radiusM),
            hookZM: lerp(a.crane?.hookZM, b.crane?.hookZM),
            windTopMs: lerp(a.wind?.gustAtTopMs, b.wind?.gustAtTopMs),
            windRefMs: lerp(a.wind?.gustAtRefMs, b.wind?.gustAtRefMs)
        )
    }
}
