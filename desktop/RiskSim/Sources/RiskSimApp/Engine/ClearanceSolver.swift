import Foundation
import simd

/// Решатель клиренса: AABB-дистанция от повёрнутого габарита груза
/// до зарегистрированных препятствий здания. Один и тот же для живого
/// режима движка и для повтора — физика одна.
final class ClearanceSolver {
    struct Box {
        var min: SIMD3<Float>
        var max: SIMD3<Float>
    }

    private(set) var boxes: [Box] = []
    /// Допустимый габарит, м (из ППР, как в Python-клиренсе).
    var margin: Float = 0.5

    func clear() { boxes.removeAll() }

    func addBox(center: SIMD3<Float>, size: SIMD3<Float>) {
        let half = size / 2
        boxes.append(Box(min: center - half, max: center + half))
    }

    /// Дистанция от осесимметричного вокруг вертикали габарита груза
    /// (envelope по yaw) до ближайшего препятствия. pos.y — высота ВЕРХА груза.
    func distance(pos: SIMD3<Float>, dims: SIMD3<Float>, yawRad: Float) -> Float {
        guard !boxes.isEmpty else { return .infinity }
        let c = SIMD3(cos(yawRad), sin(yawRad), 0)
        let s = SIMD3(-sin(yawRad), cos(yawRad), 0)
        let L = dims.x, W = dims.z, H = dims.y
        let ex = abs(L * c.x) + abs(W * s.x)
        let ez = abs(L * c.y) + abs(W * s.y)
        let half = SIMD3(ex / 2, H / 2, ez / 2)
        let center = SIMD3(pos.x, pos.y - H / 2, pos.z)
        var best = Float.infinity
        for b in boxes {
            // классическая AABB-дистанция: max(bMin - aMax, aMin - bMax, 0) по осям
            let gapLow = b.min - (center + half)
            let gapHigh = (center - half) - b.max
            let gap = simd.max(simd.max(gapLow, gapHigh), SIMD3(repeating: 0))
            let d = simd.length(gap)
            if d < best { best = d }
        }
        return best
    }
}
