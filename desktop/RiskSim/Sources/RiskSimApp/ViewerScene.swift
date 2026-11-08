import SwiftUI
import SceneKit
import simd

// MARK: - Хост сцены: построение + покадровое обновление

@MainActor
final class ViewerSceneHost: ObservableObject {
    let scene = SCNScene()
    private let root = SCNNode()          // Z-up данные
    private var slew: SCNNode?
    private var trolley: SCNNode?
    private var hook: SCNNode?
    private var load: SCNNode?
    private var loadBody: SCNNode?
    private var kindNodes: [String: SCNNode] = [:]
    private var lastKind: String?
    private let craneMeta: Crane

    // живой клиренс: решатель AABB (движок) + HUD-колбэк
    let solver = ClearanceSolver()
    var onClearance: ((String) -> Void)?
    /// текущий активный тип груза (для живого режима/перетаскивания)
    private(set) var activeKind: String = "panel"
    private(set) var loadAreaM2: Double = 3.0
    private(set) var loadMassKg: Double = 450.0
    private var loadBodyOK: SCNMaterial?
    private var loadBodyBad: SCNMaterial?
    private var lastViolation: Bool?

    // подсветка активных нарушений
    private var zoneNodes: [String: (node: SCNNode, base: NSColor)] = [:]
    private var hazardWindows: [(start: Date, end: Date, zones: Set<String>)] = []
    private var lastActiveZones: Set<String> = []
    let cameraNode = SCNNode()

    private var bundleLookup: ((String) -> Double?)?
    private var liftMass: [String: Double] = [:]
    private let frames: [TimelineFrame]
    private let bundle: SceneBundle

    init(bundle: SceneBundle) {
        craneMeta = bundle.timeline.crane
        frames = bundle.timeline.frames
        self.bundle = bundle
        for lf in bundle.lifts {
            liftMass[lf.liftId] = lf.input.massKg
        }
        bundleLookup = { [weak self] lid in
            guard let self else { return nil }
            let lf = bundle.lifts.first { $0.liftId == lid }
            return lf.map { Double($0.input.areaM2) }
        }
        scene.background.contents = NSColor(white: 0.93, alpha: 1)
        root.eulerAngles = SCNVector3(-Double.pi / 2, 0, 0)   // Z-up → Y-up
        scene.rootNode.addChildNode(root)
        build(bundle: bundle)
        setupLights()
        setupCamera()
    }

    private func material(_ r: Float, _ g: Float, _ b: Float,
                          metal: Float = 0.0, alpha: Float = 1.0) -> SCNMaterial {
        let m = SCNMaterial()
        m.lightingModel = .physicallyBased
        m.diffuse.contents = NSColor(srgbRed: CGFloat(r), green: CGFloat(g),
                                     blue: CGFloat(b), alpha: CGFloat(alpha))
        m.metalness.contents = CGFloat(metal)
        m.roughness.contents = 0.55
        if alpha < 1 {
            m.transparencyMode = .aOne
            m.blendMode = .alpha
            m.writesToDepthBuffer = false
        }
        return m
    }

    private func box(_ parent: SCNNode, name: String, size: vector_float3,
                     at pos: vector_float3, mat: SCNMaterial, obstacle: Bool = false) -> SCNNode {
        let node = SCNNode(geometry: SCNBox(width: CGFloat(size.x), height: CGFloat(size.y),
                                            length: CGFloat(size.z), chamferRadius: 0))
        node.name = name
        node.geometry?.materials = [mat]
        node.position = SCNVector3(pos.x, pos.y, pos.z)
        parent.addChildNode(node)
        if obstacle {
            solver.addBox(center: SIMD3(pos.x, pos.y, pos.z),
                          size: SIMD3(size.x, size.y, size.z))
        }
        return node
    }

    private func build(bundle: SceneBundle) {
        let tl = bundle.timeline
        let zones = tl.zones
        let crane = tl.crane
        let mx = Float(crane.mast?.x ?? 0), my = Float(crane.mast?.y ?? 0)
        let top = Float(crane.mast?.topZ ?? 288)
        let jl = Float(crane.jib.lengthM)
        let cjl = Float(crane.jib.counterJibM ?? Double(jl / 3))

        _ = box(root, name: "Ground", size: [340, 0.35, 300], at: [45, -0.175, 25],
                mat: material(0.30, 0.22, 0.14))
        _ = box(root, name: "Hardstand", size: [150, 0.25, 110], at: [45, 0.125, 25],
                mat: material(0.33, 0.33, 0.32))

        // здание
        var levels: [String: [Zone]] = [:]
        for z in zones where z.level != "G" && z.type != "crane_hazard" {
            levels[z.level, default: []].append(z)
        }
        let allBuild = levels.values.flatMap { $0 }
        if !allBuild.isEmpty {
            // footprint по объединению всех строящихся ярусов — как в GLB/Python
            let zs = allBuild
            let bx0 = Float(zs.map(\.xMin).min()! - 1.5), bx1 = Float(zs.map(\.xMax).max()! + 1.5)
            let by0 = Float(zs.map(\.yMin).min()! - 1.5), by1 = Float(zs.map(\.yMax).max()! + 1.5)
            let lowest = Float(zs.map(\.zFloor).min()!)
            var fz: Float = 4.0
            var i = 0
            while fz < lowest - 1 {
                _ = box(root, name: "Slab_\(i)", size: [bx1 - bx0 - 0.6, 0.35, by1 - by0 - 0.6],
                        at: [(bx0 + bx1) / 2, fz - 0.175, (by0 + by1) / 2],
                        mat: material(0.55, 0.55, 0.53), obstacle: true)
                _ = box(root, name: "Facade_\(i)", size: [bx1 - bx0, 4.0, by1 - by0],
                        at: [(bx0 + bx1) / 2, fz + 2.0, (by0 + by1) / 2],
                        mat: material(0.14, 0.20, 0.30, metal: 0.3, alpha: 0.94), obstacle: true)
                fz += 4.0
                i += 1
            }
            if let core = zs.first(where: { $0.id.contains("CORE") }) {
                let h = Float(core.zTop - core.zFloor)
                _ = box(root, name: "Core", size: [Float(core.xMax - core.xMin), Float(h),
                                                   Float(core.yMax - core.yMin)],
                        at: [Float((core.xMin + core.xMax) / 2), Float(core.zFloor) + Float(h) / 2,
                             Float((core.yMin + core.yMax) / 2)],
                        mat: material(0.60, 0.58, 0.55), obstacle: true)
            }
            for (name, lzs) in levels {
                guard let z0 = lzs.map(\.zFloor).min() else { continue }
                let x0 = Float(lzs.map(\.xMin).min()! - 1.5), x1 = Float(lzs.map(\.xMax).max()! + 1.5)
                let y0 = Float(lzs.map(\.yMin).min()! - 1.5), y1 = Float(lzs.map(\.yMax).max()! + 1.5)
                _ = box(root, name: "SlabTop_\(name)", size: [x1 - x0, 0.5, y1 - y0],
                        at: [(x0 + x1) / 2, Float(z0) - 0.25, (y0 + y1) / 2],
                        mat: material(0.55, 0.55, 0.53))
            }
        }

        // зоны рисков (узлы сохраняются для подсветки активных нарушений)
        for z in zones {
            let c = RiskSimZonePalette.color(for: z.type)
            let h = min(max(0.8, Float(z.zTop - z.zFloor)), 3.0)
            let node = box(root, name: "Zone_\(z.id)",
                    size: [Float(z.xMax - z.xMin), h, Float(z.yMax - z.yMin)],
                    at: [Float((z.xMin + z.xMax) / 2), Float(z.zFloor) + h / 2,
                         Float((z.yMin + z.yMax) / 2)],
                    mat: material(c.0, c.1, c.2, alpha: 0.32))
            zoneNodes[z.id] = (node, NSColor(srgbRed: CGFloat(c.0), green: CGFloat(c.1),
                                             blue: CGFloat(c.2), alpha: 1))
        }
        // окна нарушений из findings
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd HH:mm"
        for f in bundle.findings {
            guard let s = df.date(from: f.start), let e = df.date(from: f.end) else { continue }
            hazardWindows.append((s, e, Set(f.zoneIds)))
        }

        // маркеры клиренса
        if let clr = bundle.clearances {
            for rec in clr.perFrame.prefix(260) {
                guard let p = rec.point, p.count == 3 else { continue }
                let c = RiskSimZonePalette.clearance(rec.status ?? "ok")
                _ = box(root, name: "Clr", size: [0.35, 0.35, 0.35],
                        at: [Float(p[0]), Float(p[1]) - 0.2, Float(p[2])],
                        mat: material(c.0, c.1, c.2))
            }
        }

        // кран
        let craneNode = SCNNode()
        craneNode.position = vec(mx, 0, my)
        root.addChildNode(craneNode)
        _ = box(craneNode, name: "MastBase", size: [4.8, 0.5, 4.8], at: [0, 0.25, 0],
                mat: material(0.60, 0.58, 0.55))
        _ = box(craneNode, name: "Mast", size: [2.0, top - 0.5, 2.0], at: [0, (top + 0.5) / 2, 0],
                mat: material(0.93, 0.52, 0.05, metal: 0.2))

        let slew = SCNNode()
        slew.position = vec(0, top, 0)
        craneNode.addChildNode(slew)
        self.slew = slew
        _ = box(slew, name: "Deck", size: [5.2, 1.1, 5.2], at: [0, 0.05, 0],
                mat: material(0.93, 0.52, 0.05, metal: 0.2))
        _ = box(slew, name: "Cab", size: [1.3, 1.8, 1.3], at: [1.85, 1.5, -1.75],
                mat: material(0.93, 0.52, 0.05, metal: 0.2))
        _ = box(slew, name: "Jib", size: [jl, 0.6, 0.3], at: [jl / 2, 0.3, 0],
                mat: material(0.93, 0.52, 0.05, metal: 0.2))
        _ = box(slew, name: "CounterJib", size: [cjl, 0.65, 0.35], at: [-cjl / 2, 0.3, 0],
                mat: material(0.93, 0.52, 0.05, metal: 0.2))
        _ = box(slew, name: "Counterweight", size: [3.2, 2.0, 3.2], at: [-cjl + 1.6, 1.6, 0],
                mat: material(0.60, 0.58, 0.55))

        let trolley = SCNNode()
        trolley.position = vec(20, -0.3, 0)
        slew.addChildNode(trolley)
        self.trolley = trolley
        _ = box(trolley, name: "TrolleyBody", size: [1.6, 0.5, 1.4], at: .zero,
                mat: material(0.62, 0.64, 0.66, metal: 0.9))

        let hook = SCNNode()
        trolley.addChildNode(hook)
        self.hook = hook
        _ = box(hook, name: "HookBlock", size: [0.7, 1.0, 0.7], at: [0, -0.5, 0],
                mat: material(0.62, 0.64, 0.66, metal: 0.9))

        // грузы всех типов
        let load = SCNNode()
        load.position = vec(mx + 8, 2, my + 8)
        load.isHidden = true
        root.addChildNode(load)
        self.load = load
        let dims: [String: vector_float3] = [
            "panel": [2.5, 2.5, 1.2],
            "rebar": [12.0, 0.8, 0.8],
            "bucket": [2.0, 2.0, 2.0],
            "section": [3.0, 1.5, 1.5],
        ]
        let bodyOK = material(0.85, 0.42, 0.10)
        let bodyBad = material(1.00, 0.10, 0.08)
        loadBodyOK = bodyOK
        loadBodyBad = bodyBad
        for (kind, d) in dims {
            let holder = SCNNode()
            holder.name = "Kind_\(kind)"
            holder.isHidden = true
            load.addChildNode(holder)
            kindNodes[kind] = holder
            let body = box(holder, name: "Body", size: d, at: [0, -d.y / 2, 0], mat: bodyOK)
            if loadBody == nil {
                loadBody = body
            }
        }

        // бригады: цилиндры в последней точке трека
        if let tracks = bundle.agents?.tracks {
            for (ci, track) in tracks.enumerated() {
                guard let last = track.points.last else { continue }
                let c = RiskSimZonePalette.crew(ci)
                let size = bundle.timeline.crews.first(where: { $0.crewId == track.crewId })?.size ?? 1
                for member in 0..<min(6, max(1, size)) {
                    let cap = SCNNode(geometry: SCNCylinder(radius: 0.32, height: 1.6))
                    cap.geometry?.materials = [material(c.0, c.1, c.2)]
                    let dx = Float((member % 3) - 1) * 1.15
                    let dy = Float((member / 3) - 1) * 1.15
                    cap.position = vec(Float(last.x) + dx, Float(last.z) + 0.8, Float(last.y) + dy)
                    root.addChildNode(cap)
                }
            }
        }
    }

    private func setupLights() {
        let ambient = SCNNode()
        ambient.light = SCNLight()
        ambient.light?.type = .ambient
        ambient.light?.intensity = 520
        ambient.light?.color = NSColor(white: 0.9, alpha: 1)
        scene.rootNode.addChildNode(ambient)

        let sun = SCNNode()
        sun.light = SCNLight()
        sun.light?.type = .directional
        sun.light?.intensity = 750
        sun.eulerAngles = SCNVector3(-Double.pi / 3, Double.pi / 5, 0)
        scene.rootNode.addChildNode(sun)
    }

    private func setupCamera() {
        let cam = SCNCamera()
        cam.zFar = 8000
        let node = cameraNode
        node.camera = cam
        let headZ = Float(craneMeta.jib.headZM ?? craneMeta.mast?.topZ ?? 288)
        let towerTop = headZ + 16
        node.position = SCNVector3(430, 640, -500)
        node.look(at: SCNVector3(60, 40, 20))
        scene.rootNode.addChildNode(node)
    }

    /// Покадровое обновление из model.currentFrame.
    func apply(model: AppModel) {
        guard let frame = model.currentFrame else { return }
        let top = Float(craneMeta.mast?.topZ ?? 288)
        let cr = frame.crane

        if let cr {
            slew?.rotation = SCNVector4(0, 1, 0, -Double(cr.slewDeg.radians))
            trolley?.position = vec(Float(min(max(cr.radiusM, 4.0), craneMeta.jib.lengthM)), -0.3, 0)
            hook?.position = vec(0, Float(cr.hookZM) - (top + 0.25), 0)
        }

        if let l0 = frame.loads.first {
            load?.position = vec(Float(l0.x), Float(l0.z), Float(l0.y))
            let theta = Float(l0.swingThetaDeg ?? 0).radians
            let slewA = Float((cr?.slewDeg ?? 0).radians)
            // наклон маятника в плоскости стрелы + разворот по slew
            load?.eulerAngles = SCNVector3(
                -Double(cos(slewA) * theta), Double(slewA), Double(sin(slewA) * theta))
            let kind = RiskSimKind.of(l0.loadName ?? "")
            activeKind = kind
            if let lift = bundleLookup?(l0.liftId) {
                loadAreaM2 = lift
                loadMassKg = liftMass[l0.liftId] ?? 450
            }
            if kind != lastKind {
                for (k, node) in kindNodes {
                    node.isHidden = (k != kind)
                }
                lastKind = kind
            }
            load?.isHidden = false

            // живой клиренс: дистанция от габарита груза до здания (AABB)
            let dims = RiskSimLoadDims.of(kind)
            let dist = solver.distance(pos: SIMD3(Float(l0.x), Float(l0.z), Float(l0.y)),
                                       dims: dims, yawRad: slewA)
            let violated = dist < 0.5
            let warn = dist < 1.0
            if violated != lastViolation {
                loadBody?.geometry?.firstMaterial = violated ? loadBodyBad : loadBodyOK
                lastViolation = violated
            }
            let label = violated ? "НАРУШЕНИЕ" : (warn ? "близко" : "норма")
            onClearance?(String(format: "Клиренс: %.2f м · %@ (допуск 0.50 м)", dist, label))
        } else {
            load?.isHidden = true
            onClearance?("Подъёмов в этот момент нет")
        }

        // подсветка активных нарушений
        var active: Set<String> = []
        if let t = Self.df.date(from: frame.t) {
            for w in hazardWindows where w.start <= t && t <= w.end {
                active.formUnion(w.zones)
            }
        }
        if active != lastActiveZones {
            for (zid, entry) in zoneNodes {
                let mat = entry.node.geometry?.firstMaterial
                if active.contains(zid) {
                    mat?.diffuse.contents = NSColor(srgbRed: 1.0, green: 0.10, blue: 0.08, alpha: 0.5)
                } else {
                    mat?.diffuse.contents = entry.base
                }
            }
            lastActiveZones = active
        }
    }

    // MARK: - Камера-орбита (собственная, для grab-режима)

    func setCamera(yaw: Double, pitch: Double, distance: Double) {
        let yawQ = simd_quatf(angle: Float(yaw.radians), axis: [0, 1, 0])
        let pitchQ = simd_quatf(angle: Float(pitch.radians), axis: [1, 0, 0])
        let q = pitchQ * yawQ
        let target = SIMD3<Float>(60, 120, 20)
        cameraNode.position = SCNVector3(q.act(target) + SIMD3(0, 0, Float(distance)))
        cameraNode.look(at: SCNVector3(target.x, target.y, target.z))
    }

    /// Точка подвеса (тележка) в координатах данных + радиальное направление.
    func pivotState(slewDeg: Double, radiusM: Double) -> (pivot: SIMD2<Float>,
                                                           radial: SIMD2<Float>,
                                                           top: Float) {
        let a = Float(slewDeg.radians)
        let mx = Float(craneMeta.mast?.x ?? 0), my = Float(craneMeta.mast?.y ?? 0)
        let r = SIMD2<Float>(cos(a), sin(a))
        return (SIMD2<Float>(mx + Float(radiusM) * r.x, my + Float(radiusM) * r.y),
                r, Float(slewDeg == 0 ? 0 : 0))
    }

    /// Живой режим: узлы из сэмпла траектории + собственный маятник движка.
    func applyLive(sample: CranePathSampler.Sample, theta: Double, length: Double,
                   kind: String, frameIndex: Int) {
        let top = Float(craneMeta.mast?.topZ ?? 288)
        let a = Float(sample.slewDeg.radians)
        slew?.rotation = SCNVector4(0, 1, 0, -Double(sample.slewDeg.radians))
        let r = Float(min(max(sample.radiusM, 4.0), craneMeta.jib.lengthM))
        trolley?.position = vec(r, -0.3, 0)
        hook?.position = vec(0, Float(sample.hookZM) - (top + 0.25), 0)

        activeKind = kind
        if let lid = frames[min(frameIndex, frames.count - 1)].loads.first?.liftId {
            setLoadProps(liftId: lid)
        }
        if kind != lastKind {
            for (k, node) in kindNodes { node.isHidden = (k != kind) }
            lastKind = kind
        }

        let pivot = pivotState(slewDeg: sample.slewDeg, radiusM: sample.radiusM)
        let along = Float(length * sin(theta))
        let bobTop = Float(sample.hookZM) - Float(length * cos(theta))
        let pos = SIMD3<Float>(pivot.pivot.x + pivot.radial.x * along, bobTop,
                               pivot.pivot.y + pivot.radial.y * along)
        load?.position = vec(pos.x, pos.y, pos.z)
        load?.eulerAngles = SCNVector3(-Double(cos(Double(a)) * theta), Double(a),
                                       Double(sin(Double(a)) * theta))
        load?.isHidden = false

        let dims = RiskSimLoadDims.of(kind)
        let dist = solver.distance(pos: pos, dims: dims, yawRad: a)
        let violated = dist < solver.margin
        if violated != lastViolation {
            loadBody?.geometry?.firstMaterial = violated ? loadBodyBad : loadBodyOK
            lastViolation = violated
        }

        var active: Set<String> = []
        if frameIndex < frames.count, let t = Self.df.date(from: frames[frameIndex].t) {
            for w in hazardWindows where w.start <= t && t <= w.end {
                active.formUnion(w.zones)
            }
        }
        if active != lastActiveZones {
            for (zid, entry) in zoneNodes {
                let mat = entry.node.geometry?.firstMaterial
                mat?.diffuse.contents = active.contains(zid)
                    ? NSColor(srgbRed: 1.0, green: 0.10, blue: 0.08, alpha: 0.5)
                    : entry.base
            }
            lastActiveZones = active
        }
    }

    /// Свойства груза для ветра/инерции живого режима.
    func setLoadProps(liftId: String) {
        if let lf = bundle.lifts.first(where: { $0.liftId == liftId }) {
            loadAreaM2 = Double(lf.input.areaM2)
            loadMassKg = lf.input.massKg
        }
    }

    private static let df: DateFormatter = {
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd HH:mm"
        return df
    }()
}


private func vec(_ x: Float, _ y: Float, _ z: Float) -> SCNVector3 {
    SCNVector3(x, y, z)
}

enum RiskSimLoadDims {
    static func of(_ kind: String) -> SIMD3<Float> {
        switch kind {
        case "rebar": return SIMD3(12.0, 0.8, 0.8)
        case "bucket": return SIMD3(2.0, 2.0, 2.0)
        case "section": return SIMD3(3.0, 1.5, 1.5)
        default: return SIMD3(2.5, 2.5, 1.2)
        }
    }
}

enum RiskSimKind {
    static func of(_ name: String) -> String {
        let n = name.lowercased()
        if n.contains("арматур") { return "rebar" }
        if n.contains("бетон") || n.contains("бадья") { return "bucket" }
        if n.contains("секц") { return "section" }
        return "panel"
    }
}

enum RiskSimZonePalette {
    static func color(for type: String) -> (Float, Float, Float) {
        switch type {
        case "work": return (0.28, 0.58, 1.00)
        case "edge": return (1.00, 0.25, 0.25)
        case "passage": return (1.00, 0.80, 0.35)
        case "storage": return (0.62, 0.30, 0.95)
        case "crane_hazard": return (1.00, 0.45, 0.15)
        default: return (0.5, 0.5, 0.5)
        }
    }

    static func clearance(_ status: String) -> (Float, Float, Float) {
        switch status {
        case "violation": return (1.0, 0.10, 0.08)
        case "warn": return (0.95, 0.70, 0.05)
        default: return (0.20, 0.85, 0.30)
        }
    }

    static func crew(_ index: Int) -> (Float, Float, Float) {
        let palette: [(Float, Float, Float)] = [
            (0.29, 0.56, 0.95), (0.91, 0.53, 0.16), (0.49, 0.91, 0.53),
            (0.87, 0.38, 0.38), (0.70, 0.44, 0.86), (0.55, 0.75, 0.30),
        ]
        return palette[index % palette.count]
    }
}


extension Double {
    var radians: Double { self * .pi / 180 }
}

extension Float {
    var radians: Float { self * .pi / 180 }
}
