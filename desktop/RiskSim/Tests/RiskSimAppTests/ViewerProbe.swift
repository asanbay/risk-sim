import Foundation
import SceneKit
import AppKit
import simd
import Testing
@testable import RiskSimApp

/// Проверка живого клиренса вьюера против данных расчёта (кадр 455 —
/// арматура у стены ядра, Python-клиренс 0.125 м).
@MainActor
@Test func viewerClearanceProbe() async throws {
    let settings = AppSettings.default()
    let data = try Data(contentsOf: settings.bundleURL)
    let bundle = try JSONDecoder().decode(SceneBundle.self, from: data)
    let host = ViewerSceneHost(bundle: bundle)
    var hud: String?
    host.onClearance = { hud = $0 }
    let m = AppModel()
    await m.loadBundle()
    m.scrub = 455
    host.apply(model: m)
    #expect(hud != nil)
    print("VIEWER_PROBE hud:", hud ?? "nil")
}

/// Офф-screen рендер сцены: подтверждает, что движок рисует площадку
/// (важно при заблокированном экране, когда SCNView на паузе).
@MainActor
@Test func viewerOffscreenRender() throws {
    let settings = AppSettings.default()
    let data = try Data(contentsOf: settings.bundleURL)
    let bundle = try JSONDecoder().decode(SceneBundle.self, from: data)
    let host = ViewerSceneHost(bundle: bundle)
    let view = SCNView(frame: NSRect(x: 0, y: 0, width: 1280, height: 720))
    view.scene = host.scene
    view.pointOfView = host.cameraNode
    view.allowsCameraControl = false
    let img = view.snapshot()
    let rep = NSBitmapImageRep(data: img.tiffRepresentation!)
    let png = rep?.representation(using: .png, properties: [:])
    try png?.write(to: URL(fileURLWithPath: "/tmp/viewer_render.png"))
    print("VIEWER_RENDER saved:", (png?.count ?? 0), "bytes")
    #expect(png?.count ?? 0 > 10_000)
}
