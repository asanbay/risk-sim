import Foundation
import SceneKit
import simd

/// Живой цикл движка: фиксированный шаг 120 Гц, кран идёт по программной
/// траектории, груз качается на собственном интеграторе (ветер + ускорение
/// подвеса), клиренс считается на каждом кадре. Режим «Повтор» играет
/// MuJoCo-кадры 1:1 — истина остаётся в Python.
@MainActor
final class LiveEngine {
    let host: ViewerSceneHost
    let path: CranePathSampler
    let sim = LivePendulumSim()

    var liveTime: Double = 0
    var speed: Double = 60            // ускорение времени недели работ
    var grabbed = false
    private var grabTheta: Double?
    private var acc: Double = 0
    private var lastPivot: SIMD2<Float>?
    private var lastPivotVel: SIMD2<Float> = .zero
    private var hudAcc: Double = 0
    var onHUD: ((String) -> Void)?
    var onFrameIndex: ((Int) -> Void)?

    init(host: ViewerSceneHost, frames: [TimelineFrame], dtMinutes: Double) {
        self.host = host
        self.path = CranePathSampler(frames: frames, dtMinutes: dtMinutes)
    }

    var durationSeconds: Double { path.durationSeconds }

    /// Захват груза курсором: мировая точка сцены → угол маятника.
    func grab(atScenePoint p: SIMD3<Float>) {
        let s = path.sample(at: liveTime)
        let pivot = host.pivotState(slewDeg: s.slewDeg, radiusM: s.radiusM)
        // сцена: (x, y=высота, z) → данные: (x, y_data=z, высота=y)
        let pData = SIMD2<Float>(p.x, p.z)
        let offset = pData - pivot.pivot
        let along = Double(offset.x * pivot.radial.x + offset.y * pivot.radial.y)
        let bobTop = s.hookZM - sim.state.length * cos(sim.state.theta)
        let drop = max(1.0, s.hookZM - bobTop)
        grabbed = true
        grabTheta = max(-87.0 * .pi / 180.0,
                        min(87.0 * .pi / 180.0, atan2(along, drop)))
    }

    func release() {
        grabbed = false
        grabTheta = nil
    }

    func tick(dtReal: Double) {
        let dt = min(0.1, max(0, dtReal))
        acc += dt * speed
        let h = LivePendulumSim.fixedH
        var steps = 0
        while acc >= h && steps < 1200 {
            stepOnce(h)
            acc -= h
            steps += 1
        }
        render()
        hudAcc += dt
        if hudAcc > 0.2 {
            hudAcc = 0
            emitHUD()
        }
    }

    private func stepOnce(_ h: Double) {
        let s = path.sample(at: liveTime)
        let pivot = host.pivotState(slewDeg: s.slewDeg, radiusM: s.radiusM)
        var aPivot = 0.0
        if let lastP = lastPivot {
            let v = (pivot.pivot - lastP) / Float(h)
            let a = (v - lastPivotVel) / Float(h)
            aPivot = Double(a.x * pivot.radial.x + a.y * pivot.radial.y)
            lastPivotVel = v
        }
        lastPivot = pivot.pivot

        let L = max(1.0, s.hookZM - currentLoadTopZ())
        let windF = grabbed ? 0 : LivePendulumSim.windForce(
            vMs: s.windTopMs, areaM2: host.loadAreaM2)
        if grabbed, let gt = grabTheta {
            let omega = (gt - sim.state.theta) / h
            sim.state.theta = gt
            sim.state.omega = omega
        } else {
            sim.step(h: h, aPivot: aPivot, length: L,
                     windForceN: windF, massKg: host.loadMassKg)
        }
        sim.state.length = L
        liveTime += h
        if liveTime > path.durationSeconds {
            liveTime = 0        // неделя зациклена
            lastPivot = nil
            lastPivotVel = .zero
        }
    }

    private func currentLoadTopZ() -> Double {
        let idx = min(path.frames.count - 1, max(0, Int(liveTime / path.dtSeconds)))
        return path.frames[idx].loads.first?.z ?? (path.frames[idx].crane?.hookZM ?? 100.0) - 10.0
    }

    private func render() {
        let s = path.sample(at: liveTime)
        let idx = min(path.frames.count - 1, max(0, Int(liveTime / path.dtSeconds)))
        let kind = path.frames[idx].loads.first.map { RiskSimKind.of($0.loadName ?? "") }
            ?? host.activeKind
        host.applyLive(sample: s,
                       theta: sim.state.theta,
                       length: sim.state.length,
                       kind: kind,
                       frameIndex: idx)
        onFrameIndex?(idx)
    }

    private func activeKindName() -> String {
        let idx = min(path.frames.count - 1, max(0, Int(liveTime / path.dtSeconds)))
        if let l0 = path.frames[idx].loads.first {
            return RiskSimKind.of(l0.loadName ?? "")
        }
        return host.activeKind
    }

    private func emitHUD() {
        let s = path.sample(at: liveTime)
        let pivot = host.pivotState(slewDeg: s.slewDeg, radiusM: s.radiusM)
        let radial = SIMD3<Float>(pivot.radial.x, 0, pivot.radial.y)
        let L = Float(sim.state.length)
        let bobTop = Float(s.hookZM) - L * Float(cos(sim.state.theta))
        let along = L * Float(sin(sim.state.theta))
        let pos = SIMD3<Float>(pivot.pivot.x + radial.x * along, bobTop,
                               pivot.pivot.y + radial.z * along)
        let dims = RiskSimLoadDims.of(host.activeKind)
        let dist = host.solver.distance(pos: pos, dims: dims, yawRad: Float(s.slewDeg.radians))
        let hh = Int(liveTime / 3600) % 24
        let mm = Int(liveTime / 60) % 60
        let day = Int(liveTime / 86400) + 1
        if dist < host.solver.margin {
            onHUD?(String(format: "⚡ НАРУШЕНИЕ %.2f м · день %d %02d:%02d", dist, day, hh, mm))
        } else {
            onHUD?(String(format: "Клиренс %.2f м · день %d %02d:%02d · ветер %.1f м/с",
                          dist, day, hh, mm, s.windTopMs))
        }
    }
}
