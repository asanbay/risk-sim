import SwiftUI
import SceneKit

/// RiskSim Engine — страница 3D с двумя режимами:
///  • Повтор (MuJoCo) — детерминированное проигрывание кадров расчёта;
///  • Живой движок — собственный интегратор маятника 120 Гц: кран идёт по
///    программной траектории, груз качается от ветра и ускорения подвеса,
///    груз можно СХВАТИТЬ мышью; клиренс считается на каждом кадре.
struct Viewer3DPage: View {
    @EnvironmentObject private var model: AppModel
    @State private var sceneHost: ViewerSceneHost?
    @State private var liveEngine: LiveEngine?
    @State private var mode: ViewerMode = .replay
    @State private var clearanceHUD: String = ""
    @State private var yaw: Double = 35
    @State private var pitch: Double = 24
    @State private var distance: Double = 620
    @State private var liveSpeed: Double = 60
    @State private var liveRunning = false
    @State private var lastTick: Date?
    @State private var liveTimer: Timer?

    enum ViewerMode: String, CaseIterable {
        case replay = "Повтор (MuJoCo)"
        case live = "Живой движок"
    }

    var body: some View {
        VStack(spacing: 10) {
            toolbar
            if model.bundle != nil, let host = sceneHost ?? makeHost() {
                ViewerSceneNSView(scene: host.scene) { view in
                    configure(view, host: host)
                }
                .overlay(alignment: .topTrailing) {
                    if !clearanceHUD.isEmpty {
                        Text(clearanceHUD)
                            .font(.callout.monospacedDigit().weight(.semibold))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(.thinMaterial, in: Capsule())
                            .padding(8)
                    }
                }
                .onChange(of: model.scrub) { _ in
                    if mode == .replay { host.apply(model: model) }
                }
                .onAppear {
                    host.apply(model: model)
                    host.setCamera(yaw: yaw, pitch: pitch, distance: distance)
                }
            } else {
                EmptyStateView("Нет результатов", systemImage: "rotate.3d",
                               description: Text("Запустите расчёт пилота"))
            }
        }
        .padding(12)
        .onDisappear { stopLive() }
    }

    private var toolbar: some View {
        HStack(spacing: 12) {
            Picker("Режим", selection: $mode) {
                ForEach(ViewerMode.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .frame(width: 320)
            .onChange(of: mode) { m in
                stopLive()
                if m == .live { startLive() }
                else { sceneHost?.apply(model: model) }
            }
            switch mode {
            case .replay:
                PlaybackControls()
                Spacer()
            case .live:
                Text("Скорость времени")
                    .font(.caption).foregroundStyle(.secondary)
                Picker("Скорость", selection: $liveSpeed) {
                    Text("×30").tag(30.0)
                    Text("×60").tag(60.0)
                    Text("×120").tag(120.0)
                    Text("×600").tag(600.0)
                }
                .pickerStyle(.segmented)
                .frame(width: 220)
                .onChange(of: liveSpeed) { _ in liveEngine?.speed = liveSpeed }
                Button(liveRunning ? "Стоп" : "Пуск") {
                    liveRunning ? stopLive() : startLive()
                }
                .buttonStyle(.borderedProminent)
                Spacer()
                Text("ЛКМ на грузе — схватить · ЛКМ на пустом — орбита · колесо — зум")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Хост и ввод

    private func makeHost() -> ViewerSceneHost? {
        guard let bundle = model.bundle else { return nil }
        let host = ViewerSceneHost(bundle: bundle)
        host.onClearance = { text in
            if mode == .replay { clearanceHUD = text }
        }
        sceneHost = host
        return host
    }

    private func configure(_ view: RiskSimSCNView, host: ViewerSceneHost) {
        view.onOrbit = { dx, dy in
            yaw -= Double(dx) * 0.25
            pitch = min(85, max(3, pitch + Double(dy) * 0.18))
            host.setCamera(yaw: yaw, pitch: pitch, distance: distance)
        }
        view.onZoom = { delta in
            distance = min(1600, max(60, distance + Double(delta) * 0.8))
            host.setCamera(yaw: yaw, pitch: pitch, distance: distance)
        }
        view.onGrabStart = {
            guard mode == .live, let engine = liveEngine else { return }
            stopLiveTimer()
        }
        view.onGrabMove = { worldPoint in
            liveEngine?.grab(atScenePoint: worldPoint)
        }
        view.onGrabEnd = {
            liveEngine?.release()
            if mode == .live, liveRunning { startLiveTimer() }
        }
    }

    // MARK: - Живой цикл

    private func startLive() {
        guard let host = sceneHost, let bundle = model.bundle else { return }
        if liveEngine == nil {
            let dt = Double(bundle.timeline.meta.dtMin ?? 10.0)
            liveEngine = LiveEngine(host: host,
                                    frames: bundle.timeline.frames,
                                    dtMinutes: dt)
            liveEngine?.speed = liveSpeed
        }
        liveEngine?.onHUD = { text in
            if mode == .live { clearanceHUD = text }
        }
        liveRunning = true
        startLiveTimer()
    }

    private func startLiveTimer() {
        lastTick = Date()
        let t = Timer(timeInterval: 1.0 / 60.0, repeats: true) { _ in
            MainActor.assumeIsolated {
                guard mode == .live, liveRunning, let engine = liveEngine else { return }
                let now = Date()
                let dt = now.timeIntervalSince(lastTick ?? now)
                lastTick = now
                engine.tick(dtReal: dt)
            }
        }
        RunLoop.main.add(t, forMode: .common)
        liveTimer = t
    }

    private func stopLive() {
        liveRunning = false
        liveEngine?.release()
        stopLiveTimer()
    }

    private func stopLiveTimer() {
        liveTimer?.invalidate()
        liveTimer = nil
        lastTick = nil
    }
}
