import Foundation
import Testing
@testable import RiskSimApp

/// Тесты живого интегратора маятника (сердце живого режима движка).
@MainActor
struct PendulumTests {
    @Test func smallAnglePeriodMatchesAnalytic() {
        let sim = LivePendulumSim()
        sim.state.theta = 0.05          // малый угол → период 2π√(L/g)
        sim.state.omega = 0
        let L = 25.0
        let h = LivePendulumSim.fixedH
        var peaks: [Double] = []
        var prev = sim.state.theta
        for step in 0..<Int(120.0 / h) {
            sim.step(h: h, aPivot: 0, length: L, windForceN: 0, massKg: 500)
            if prev < 0 && sim.state.theta >= 0 {
                peaks.append(Double(step) * h)
            }
            prev = sim.state.theta
        }
        let analytic = 2 * Double.pi * (L / sim.g).squareRoot()
        let measured = peaks.count >= 2 ? peaks[1] - peaks[0] : analytic
        #expect(abs(measured - analytic) < 0.15)
    }

    @Test func windPushesLoadAlongForce() {
        let sim = LivePendulumSim()
        sim.state.theta = 0
        sim.state.omega = 0
        for _ in 0..<120 {
            sim.step(h: LivePendulumSim.fixedH, aPivot: 0, length: 10,
                     windForceN: 800, massKg: 450)
        }
        #expect(sim.state.theta > 0.01)   // ветер снёс груз в сторону силы
    }

    @Test func dampingDecaysAmplitude() {
        let sim = LivePendulumSim()
        sim.state.theta = 0.6
        sim.state.omega = 0
        for _ in 0..<Int(60.0 / LivePendulumSim.fixedH) {
            sim.step(h: LivePendulumSim.fixedH, aPivot: 0, length: 15,
                     windForceN: 0, massKg: 500)
        }
        #expect(abs(sim.state.theta) < 0.3)   // качание затухло
    }

    @Test func pivotAccelSwingsLoadOpposite() {
        let sim = LivePendulumSim()
        sim.state.theta = 0
        sim.state.omega = 0
        for _ in 0..<60 {
            sim.step(h: LivePendulumSim.fixedH, aPivot: -2.0, length: 10,
                     windForceN: 0, massKg: 450)
        }
        #expect(sim.state.theta > 0)   // рывок подвеса влево качает груз вправо
    }
}
