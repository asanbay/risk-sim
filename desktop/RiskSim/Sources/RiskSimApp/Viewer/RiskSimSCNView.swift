import SwiftUI
import SceneKit

/// Собственный SCNView движка: ЛКМ на грузе — захват (живой режим),
/// ЛКМ на пустом месте — орбита, колесо — зум. Это часть устройства
/// симуляции: ввод напрямую управляет состоянием движка.
final class RiskSimSCNView: SCNView {
    var onOrbit: ((CGFloat, CGFloat) -> Void)?
    var onZoom: ((CGFloat) -> Void)?
    var onGrabStart: (() -> Void)?
    var onGrabMove: ((SIMD3<Float>) -> Void)?   // мировая точка курсора (сцена)
    var onGrabEnd: (() -> Void)?

    private var orbiting = false
    private var grabbing = false
    private var lastPoint = NSPoint.zero

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        lastPoint = p
        if hitsLoad(p) {
            grabbing = true
            onGrabStart?()
        } else {
            orbiting = true
        }
    }

    override func mouseDragged(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        let dx = p.x - lastPoint.x
        let dy = p.y - lastPoint.y
        lastPoint = p
        if grabbing {
            onGrabMove?(worldPointUnder(p))
        } else if orbiting {
            onOrbit?(dx, dy)
        }
    }

    override func mouseUp(with event: NSEvent) {
        if grabbing {
            grabbing = false
            onGrabEnd?()
        }
        orbiting = false
    }

    override func scrollWheel(with event: NSEvent) {
        onZoom?(CGFloat(event.scrollingDeltaY))
    }

    override func magnify(with event: NSEvent) {
        onZoom?(CGFloat(-event.magnification * 40))
    }

    private func hitsLoad(_ p: NSPoint) -> Bool {
        guard let hit = hitTest(p, options: [.searchMode: SCNHitTestSearchMode.closest.rawValue,
                                             .ignoreHiddenNodes: true]).first else { return false }
        var node: SCNNode? = hit.node
        while let n = node {
            if n.name?.hasPrefix("Kind_") == true || n.name == "Load" {
                return true
            }
            node = n.parent
        }
        return false
    }

    /// Точка на луче курсора: near/far unproject.
    func cursorRay(_ p: NSPoint) -> (near: SCNVector3, far: SCNVector3) {
        (unprojectPoint(SCNVector3(p.x, p.y, 0.0)),
         unprojectPoint(SCNVector3(p.x, p.y, 1.0)))
    }

    private func worldPointUnder(_ p: NSPoint) -> SIMD3<Float> {
        let ray = cursorRay(p)
        let near = SIMD3<Float>(Float(ray.near.x), Float(ray.near.y), Float(ray.near.z))
        let far = SIMD3<Float>(Float(ray.far.x), Float(ray.far.y), Float(ray.far.z))
        return near + (far - near) * 0.5
    }
}

/// Мост SwiftUI → SCNView.
struct ViewerSceneNSView: NSViewRepresentable {
    let scene: SCNScene
    let configure: (RiskSimSCNView) -> Void

    func makeNSView(context: Context) -> RiskSimSCNView {
        let view = RiskSimSCNView()
        view.scene = scene
        view.antialiasingMode = .multisampling4X
        view.rendersContinuously = true
        configure(view)
        return view
    }

    func updateNSView(_ nsView: RiskSimSCNView, context: Context) {}
}
