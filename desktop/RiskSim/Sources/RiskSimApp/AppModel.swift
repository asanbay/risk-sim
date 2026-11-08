import AppKit
import Foundation
import UniformTypeIdentifiers

struct ProcessResult {
    let status: Int32
    let output: String
}

enum BridgeError: LocalizedError {
    case missingPython(String)
    case processFailed(Int32, String)
    case missingFile(String)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .missingPython(let path): return "Python-окружение не найдено: \(path)"
        case .processFailed(let status, let output): return "Команда завершилась с кодом \(status):\n\(output)"
        case .missingFile(let path): return "Файл не найден: \(path)"
        case .cancelled: return "Команда отменена"
        }
    }
}

/// Хранилище запущенного процесса: позволяет отменить расчёт из интерфейса.
final class ProcessHandle: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?

    func store(_ process: Process?) {
        lock.lock()
        defer { lock.unlock() }
        self.process = process
    }

    func terminate() {
        lock.lock()
        defer { lock.unlock() }
        if let process, process.isRunning {
            process.terminate()
        }
    }
}

/// Разбивает поток байтов на строки, не разрывая многобайтовый UTF-8,
/// и хранит полный вывод для диагностики ошибок.
final class LineBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var pending = Data()
    private var collected = Data()

    /// Возвращает готовые строки и оставляет остаток в буфере.
    func append(_ chunk: Data) -> [String] {
        lock.lock()
        defer { lock.unlock() }
        pending.append(chunk)
        collected.append(chunk)
        var lines: [String] = []
        while let idx = pending.firstIndex(of: 0x0A) {
            let lineData = pending.prefix(idx)
            pending.removeSubrange(...idx)
            if let line = String(data: lineData, encoding: .utf8) {
                lines.append(line.trimmingCharacters(in: .whitespaces))
            }
        }
        return lines
    }

    /// Остаток без завершающего перевода строки (конец потока).
    func flush() -> String? {
        lock.lock()
        defer { lock.unlock() }
        guard !pending.isEmpty else { return nil }
        defer { pending.removeAll() }
        return String(data: pending, encoding: .utf8)?.trimmingCharacters(in: .whitespaces)
    }

    /// Полный вывод процесса (для сообщения об ошибке).
    func fullText() -> String {
        lock.lock()
        defer { lock.unlock() }
        return String(data: collected, encoding: .utf8) ?? ""
    }
}

final class PythonBridge: @unchecked Sendable {
    let handle = ProcessHandle()

    /// Запускает Python CLI (по умолчанию) или внешнюю программу (Blender и т.п.).
    /// `onLine` вызывается в фоновом потоке — переключение на MainActor на вызывающем.
    func run(
        _ arguments: [String],
        settings: AppSettings,
        program: URL? = nil,
        environment: [String: String]? = nil,
        onLine: (@Sendable (String) -> Void)? = nil
    ) async throws -> ProcessResult {
        try await Task.detached(priority: .userInitiated) { [handle] in
            let executable: URL
            let finalArguments: [String]
            if let program {
                executable = program
                finalArguments = arguments
            } else {
                let python = settings.pythonURL
                guard FileManager.default.isExecutableFile(atPath: python.path) else {
                    throw BridgeError.missingPython(python.path)
                }
                executable = python
                finalArguments = ["-m", "risk_sim.cli"] + arguments
            }
            let process = Process()
            process.executableURL = executable
            process.arguments = finalArguments
            if let environment {
                var merged = ProcessInfo.processInfo.environment
                for (key, value) in environment {
                    merged[key] = value
                }
                process.environment = merged
            }
            process.currentDirectoryURL = settings.projectURL
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            let buffer = LineBuffer()
            pipe.fileHandleForReading.readabilityHandler = { reading in
                let chunk = reading.availableData
                if chunk.isEmpty {
                    reading.readabilityHandler = nil
                    return
                }
                for line in buffer.append(chunk) where !line.isEmpty {
                    onLine?(line)
                }
            }
            do {
                try process.run()
            } catch {
                pipe.fileHandleForReading.readabilityHandler = nil
                throw error
            }
            handle.store(process)
            if Task.isCancelled {
                process.terminate()
            }
            process.waitUntilExit()
            handle.store(nil)
            pipe.fileHandleForReading.readabilityHandler = nil
            if let tail = buffer.flush(), !tail.isEmpty {
                onLine?(tail)
            }
            let output = buffer.fullText()
            let result = ProcessResult(status: process.terminationStatus, output: output)
            if Task.isCancelled || (process.terminationReason == .uncaughtSignal && result.status != 0) {
                throw BridgeError.cancelled
            }
            if result.status != 0 {
                let tail = String(output.suffix(2000))
                throw BridgeError.processFailed(result.status, tail)
            }
            return result
        }.value
    }
}

enum AppPage: String, CaseIterable, Identifiable {
    case briefing = "Брифинг"
    case overview = "Обзор"
    case viewer3D = "3D"
    case plan = "План"
    case conflicts = "Конфликты"
    case lifts = "Подъёмы"
    case timeline = "Таймлайн"
    case reports = "Отчёты"
    case settings = "Настройки"

    var id: String { rawValue }
    var icon: String {
        switch self {
        case .briefing: return "sun.max.fill"
        case .overview: return "gauge"
        case .viewer3D: return "rotate.3d"
        case .plan: return "map"
        case .conflicts: return "exclamationmark.triangle"
        case .lifts: return "crane"
        case .timeline: return "clock"
        case .reports: return "doc.text"
        case .settings: return "gearshape"
        }
    }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var settings: AppSettings
    @Published var bundle: SceneBundle?
    /// Последняя открытая страница сохраняется между запусками.
    @Published var selectedPage: AppPage {
        didSet { UserDefaults.standard.set(selectedPage.rawValue, forKey: "RiskSim.SelectedPage") }
    }
    @Published var isBusy = false
    @Published var errorMessage: String?
    @Published var log = [String]()
    /// Позиция скраббинга сохраняется между запусками.
    @Published var scrub: Double = UserDefaults.standard.double(forKey: "RiskSim.Scrub") {
        didSet { UserDefaults.standard.set(scrub, forKey: "RiskSim.Scrub") }
    }
    @Published var isPlaying = false
    @Published var playbackSpeed = 8.0

    private let bridge = PythonBridge()
    private var runTask: Task<Void, Never>?
    private var playbackTask: Task<Void, Never>?

    init() {
        settings = .load()
        let stored = UserDefaults.standard.string(forKey: "RiskSim.SelectedPage").flatMap(AppPage.init(rawValue:))
        _selectedPage = Published(initialValue: stored ?? .briefing)
        Task { await loadBundle() }
    }

    var projectTitle: String {
        bundle?.timeline.meta.project ?? "RiskSim — цифровой двойник работ"
    }

    var criticalCount: Int {
        bundle?.findings.filter { $0.severity == "critical" }.count ?? 0
    }

    var frameCount: Int { bundle?.timeline.frames.count ?? 0 }
    var liftCount: Int { bundle?.lifts.count ?? 0 }

    var currentFrame: TimelineFrame? {
        guard let frames = bundle?.timeline.frames, !frames.isEmpty else { return nil }
        let index = min(max(Int(scrub.rounded()), 0), frames.count - 1)
        return frames[index]
    }

    var dataWarnings: [String] {
        bundle?.context?.warnings ?? []
    }

    var findingsBySeverity: [String: Int] {
        if let stored = bundle?.findingsSummary?.bySeverity, !stored.isEmpty { return stored }
        var counts: [String: Int] = [:]
        for finding in bundle?.findings ?? [] {
            counts[finding.severity, default: 0] += 1
        }
        return counts
    }

    var pythonReady: Bool {
        FileManager.default.isExecutableFile(atPath: settings.pythonURL.path)
    }

    func loadBundle() async {
        do {
            let url = settings.bundleURL
            guard FileManager.default.fileExists(atPath: url.path) else {
                bundle = nil
                return
            }
            let data = try Data(contentsOf: url)
            let decoded = try JSONDecoder().decode(SceneBundle.self, from: data)
            bundle = decoded
            scrub = min(scrub, Double(max(decoded.timeline.frames.count - 1, 0)))
        } catch {
            errorMessage = "Не удалось прочитать scene_bundle.json: \(error.localizedDescription)"
        }
    }

    /// Полный расчёт: MuJoCo-раскачка, полный Monte Carlo, GIF-анимация.
    func runPilot() {
        startRun(["pilot", "--data", settings.dataPath, "--output", settings.outputPath,
                  "--swing-engine", "mujoco", "--gif"],
                 successMessage: "Расчёт пилота завершён")
    }

    /// Быстрый расчёт: грубее Monte Carlo и шаг таймлайна, без GIF и вьюера.
    func runPilotFast() {
        startRun(["pilot", "--data", settings.dataPath, "--output", settings.outputPath,
                  "--swing-engine", "mujoco", "--mc-lifts", "60", "--timeline-dt", "20",
                  "--no-viewer"],
                 successMessage: "Быстрый расчёт завершён")
    }

    func cancelRun() {
        bridge.handle.terminate()
        runTask?.cancel()
    }

    func validatePhysics() {
        startRun(["validate", "--data", "data"], successMessage: "Проверка физики пройдена")
    }

    /// Реалистичное видео в Blender: сцена -> анимация -> рендер Cycles.
    func renderRealisticVideo() {
        let script = settings.projectURL.appendingPathComponent("blender/render_realistic.sh")
        guard FileManager.default.fileExists(atPath: script.path) else {
            errorMessage = "Не найден blender/render_realistic.sh в корне проекта"
            return
        }
        guard FileManager.default.isExecutableFile(atPath: settings.blenderPath) else {
            errorMessage = "Blender не найден: \(settings.blenderPath) — проверьте настройки"
            return
        }
        let bundle = settings.bundleURL
        guard FileManager.default.fileExists(atPath: bundle.path) else {
            errorMessage = "Сначала запустите расчёт: нет scene_bundle.json"
            return
        }
        let environment = [
            "BUNDLE": bundle.path,
            "SCENE": settings.outputURL.appendingPathComponent("crane_scene.blend").path,
            "OUT": settings.outputURL.appendingPathComponent("simulation_realistic.mp4").path,
            "BLENDER": settings.blenderPath,
            "ENGINE": "CYCLES",
            "EVERY": "8",
            "FPS": "24",
            "WIDTH": "1280",
            "HEIGHT": "720",
            "SAMPLES": "64",
        ]
        Task { await execute(
            [script.path],
            program: URL(fileURLWithPath: "/bin/bash"),
            environment: environment,
            successMessage: "Реалистичное видео готово: simulation_realistic.mp4"
        ) }
    }

    func openRealisticVideo() {
        let url = settings.outputURL.appendingPathComponent("simulation_realistic.mp4")
        guard FileManager.default.fileExists(atPath: url.path) else {
            errorMessage = "Ролик ещё не создан — сначала «Реалистичное видео»"
            return
        }
        NSWorkspace.shared.open(url)
    }

    func refresh() {
        Task { await loadBundle() }
    }

    /// Автосохранение после правки полей настроек + перезагрузка данных.
    func settingsEdited(reloadBundle: Bool) {
        settings.save()
        if reloadBundle {
            Task { await loadBundle() }
        }
    }

    // MARK: - Воспроизведение таймлайна

    func togglePlayback() {
        isPlaying ? stopPlayback() : startPlayback()
    }

    func startPlayback() {
        guard frameCount > 1, playbackTask == nil else { return }
        if scrub >= Double(frameCount - 1) { scrub = 0 }
        isPlaying = true
        playbackTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let interval = UInt64(1_000_000_000 / max(self.playbackSpeed, 1))
                try? await Task.sleep(nanoseconds: interval)
                if Task.isCancelled { break }
                self.advanceFrame()
            }
        }
    }

    func stopPlayback() {
        playbackTask?.cancel()
        playbackTask = nil
        isPlaying = false
    }

    private func advanceFrame() {
        guard frameCount > 1 else { return }
        scrub = scrub >= Double(frameCount - 1) ? 0 : min(scrub + 1, Double(frameCount - 1))
    }

    // MARK: - Выбор путей

    private func pickDirectory(title: String) -> URL? {
        let panel = NSOpenPanel()
        panel.title = title
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK else { return nil }
        return panel.url
    }

    private func relativize(_ url: URL) -> String {
        let project = settings.projectURL.standardizedFileURL.path
        let selected = url.standardizedFileURL.path
        if selected.hasPrefix(project + "/") {
            return String(selected.dropFirst(project.count + 1))
        }
        return selected
    }

    func chooseProjectRoot() {
        guard let url = pickDirectory(title: "Корень проекта risk-sim") else { return }
        settings.projectPath = url.standardizedFileURL.path
        settingsEdited(reloadBundle: true)
    }

    func chooseDataDirectory() {
        guard let url = pickDirectory(title: "Папка данных проекта") else { return }
        settings.dataPath = relativize(url)
        settingsEdited(reloadBundle: true)
    }

    func chooseOutputDirectory() {
        guard let url = pickDirectory(title: "Папка результатов") else { return }
        settings.outputPath = relativize(url)
        settingsEdited(reloadBundle: true)
    }

    func chooseBlenderBinary() {
        let panel = NSOpenPanel()
        panel.title = "Исполняемый файл Blender"
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        settings.blenderPath = url.path
        settingsEdited(reloadBundle: false)
    }

    func chooseIFC() {
        let panel = NSOpenPanel()
        panel.title = "IFC-модель здания"
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [UTType(filenameExtension: "ifc") ?? .data]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        startRun(["import-ifc", url.path, "--data", settings.dataPath],
                 successMessage: "IFC импортирован. Теперь запустите расчёт пилота")
    }

    // MARK: - Открытие артефактов

    func openBlender() {
        let candidates = [
            settings.outputURL.appendingPathComponent("crane_scene.blend"),
            settings.outputURL.appendingPathComponent("usd/stage.blend"),
            settings.outputURL.appendingPathComponent("preview.blend"),
            settings.outputURL.appendingPathComponent("building.blend"),
        ]
        guard let scene = candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }) else {
            errorMessage = "Сцена .blend ещё не создана"
            return
        }
        let application = URL(fileURLWithPath: settings.blenderPath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        NSWorkspace.shared.open([scene], withApplicationAt: application, configuration: .init(), completionHandler: nil)
    }

    func openReport() {
        guard FileManager.default.fileExists(atPath: settings.reportURL.path) else {
            errorMessage = "PDF-отчёт ещё не создан"
            return
        }
        NSWorkspace.shared.open(settings.reportURL)
    }

    func openDOCX() {
        let url = settings.outputURL.appendingPathComponent("report.docx")
        guard FileManager.default.fileExists(atPath: url.path) else {
            errorMessage = "DOCX-отчёт ещё не создан"
            return
        }
        NSWorkspace.shared.open(url)
    }

    func openOutputFolder() {
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: settings.outputURL.path)
    }

    func openUSD() {
        let url = settings.outputURL.appendingPathComponent("usd/stage_flattened.usda")
        guard FileManager.default.fileExists(atPath: url.path) else {
            errorMessage = "Сначала выполните export-usd --flatten"
            return
        }
        NSWorkspace.shared.selectFile(url.path, inFileViewerRootedAtPath: url.deletingLastPathComponent().path)
    }

    // MARK: - Запуск команд

    private func startRun(
        _ arguments: [String],
        program: URL? = nil,
        environment: [String: String]? = nil,
        successMessage: String
    ) {
        guard !isBusy else { return }
        runTask = Task { await execute(arguments, program: program, environment: environment,
                                       successMessage: successMessage) }
    }

    private func execute(
        _ arguments: [String],
        program: URL? = nil,
        environment: [String: String]? = nil,
        successMessage: String
    ) async {
        guard !isBusy else { return }
        isBusy = true
        errorMessage = nil
        stopPlayback()
        let label = program == nil
            ? "$ risk-sim \(arguments.joined(separator: " "))"
            : "$ \(program!.lastPathComponent) \(arguments.joined(separator: " "))"
        appendLog(label)
        do {
            _ = try await bridge.run(arguments, settings: settings,
                                     program: program, environment: environment) { [weak self] line in
                guard !line.isEmpty else { return }
                Task { @MainActor [weak self] in self?.appendLog(line) }
            }
            appendLog(successMessage)
            await loadBundle()
        } catch {
            if case BridgeError.cancelled = error {
                appendLog("Команда отменена пользователем")
            } else {
                errorMessage = error.localizedDescription
                appendLog(error.localizedDescription)
            }
        }
        runTask = nil
        isBusy = false
    }

    private func appendLog(_ message: String) {
        let stamp = DateFormatter.localizedString(from: .now, dateStyle: .none, timeStyle: .medium)
        log.append("[\(stamp)] \(message)")
        if log.count > 400 { log.removeFirst(log.count - 400) }
    }
}
