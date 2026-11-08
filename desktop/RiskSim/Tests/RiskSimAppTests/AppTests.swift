import Foundation
import Testing
@testable import RiskSimApp

@Test func defaultProjectPathsExist() {
    let settings = AppSettings.default()
    #expect(FileManager.default.fileExists(atPath: settings.projectURL.appendingPathComponent("risk_sim/cli.py").path))
    #expect(FileManager.default.isExecutableFile(atPath: settings.pythonURL.path))
    #expect(FileManager.default.fileExists(atPath: settings.blenderPath))
}

@Test func pythonBridgeValidation() async throws {
    let result = try await PythonBridge().run(["validate", "--data", "data"], settings: .default())
    #expect(result.status == 0)
    #expect(result.output.contains("\"all_passed\": true"))
}

@Test func sceneBundleDecoding() throws {
    let settings = AppSettings.default()
    let data = try Data(contentsOf: settings.bundleURL)
    let bundle = try JSONDecoder().decode(SceneBundle.self, from: data)
    #expect(bundle.lifts.count == 6)
    #expect(bundle.findings.count == 63)
    #expect(bundle.timeline.frames.count == 937)
    #expect(bundle.timeline.crews.count == 6)
    // Новые блоки, на которые опирается интерфейс
    #expect(bundle.findingsSummary?.total == 63)
    #expect(bundle.monteCarlo?.lifts.count == 6)
    #expect(bundle.monteCarlo?.schedule != nil)
    #expect(bundle.agents?.summary != nil)
    #expect(bundle.timeline.crane.mast != nil)
    #expect(bundle.timeline.frames.contains { $0.tasksActive?.isEmpty == false })
    #expect(bundle.lifts.contains { $0.violations?.isEmpty == false })
    #expect(bundle.lifts.contains { ($0.summary.safeWindows ?? []).isEmpty == false })
    // Клиренс грузового пути против модели
    #expect(bundle.model != nil)
    #expect(bundle.clearances?.events.isEmpty == false)
    #expect(bundle.clearances?.marginM == 0.5)
}

@MainActor
@Test func modelLoadsCurrentResults() async {
    let model = AppModel()
    await model.loadBundle()
    #expect(model.criticalCount == 43)
    #expect(model.liftCount == 6)
    #expect(model.frameCount == 937)
    #expect(model.currentFrame != nil)
}

@Test func pilotSmokeInTemporaryDirectory() async throws {
    let output = FileManager.default.temporaryDirectory
        .appendingPathComponent("risk-sim-swift-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: output) }

    var settings = AppSettings.default()
    settings.outputPath = output.path
    let result = try await PythonBridge().run([
        "pilot", "--data", "data/sample", "--output", output.path,
        "--mc-lifts", "20", "--mc-schedule", "0", "--skip-lift-swing",
        "--agent-dt", "60", "--timeline-dt", "60", "--no-report", "--no-viewer"
    ], settings: settings)
    #expect(result.status == 0)
    let bundle = try JSONDecoder().decode(SceneBundle.self, from: Data(contentsOf: settings.bundleURL))
    #expect(bundle.lifts.count == 6)
    #expect(!bundle.timeline.frames.isEmpty)
}
