import SwiftUI

/// Секретная страница утреннего брифинга: ночной кинематографичный рендер
/// встречает инженера цифрой за день.
struct BriefingView: View {
    @EnvironmentObject private var model: AppModel
    @State private var heroImage: NSImage?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                heroSection
                if let day = model.briefingDay {
                    daySummarySection(day)
                }
                artifactsSection
            }
            .padding(18)
        }
        .onAppear { loadHero() }
    }

    // MARK: - Hero

    private var heroSection: some View {
        ZStack(alignment: .bottomLeading) {
            Group {
                if let heroImage {
                    Image(nsImage: heroImage)
                        .resizable()
                        .scaledToFill()
                } else {
                    Rectangle()
                        .fill(LinearGradient(colors: [Color.orange.opacity(0.5),
                                                      Color.purple.opacity(0.4)],
                                             startPoint: .topLeading, endPoint: .bottomTrailing))
                }
            }
            .frame(height: 320)
            .frame(maxWidth: .infinity)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 14))

            VStack(alignment: .leading, spacing: 4) {
                Text(greeting)
                    .font(.title.weight(.bold))
                    .foregroundStyle(.white)
                Text("\(model.projectTitle) · \(model.liftCount) подъёма · \(model.frameCount) кадров недели")
                    .font(.callout)
                    .foregroundStyle(.white.opacity(0.9))
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                LinearGradient(colors: [.clear, .black.opacity(0.65)],
                               startPoint: .center, endPoint: .bottom)
            )
            .clipShape(RoundedRectangle(cornerRadius: 14))
        }
    }

    private var greeting: String {
        let hour = Calendar.current.component(.hour, from: Date())
        switch hour {
        case 5..<12: return "Доброе утро"
        case 12..<18: return "Добрый день"
        case 18..<23: return "Добрый вечер"
        default: return "Ночная смена"
        }
    }

    // MARK: - Сводка дня

    private func daySummarySection(_ day: BriefingDay) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Смена \(day.dateLabel)")
                .font(.title3.weight(.semibold))
            HStack(spacing: 12) {
                briefCard("Подъёмов", "\(day.liftCount)", .blue)
                briefCard("Нарушений", "\(day.findingCount)", day.findingCount > 0 ? .red : .green)
                briefCard("Критических", "\(day.criticalCount)", day.criticalCount > 0 ? .red : .green)
                briefCard("Безопасных окон", "\(day.safeWindowCount)", .green)
            }
            if !day.topMessages.isEmpty {
                GroupBox("На что смотреть") {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(day.topMessages, id: \.self) { msg in
                            Label(msg, systemImage: "exclamationmark.triangle.fill")
                                .font(.callout)
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if let worst = model.bundle?.clearances?.worst {
                GroupBox("Клиренс недели") {
                    HStack(spacing: 16) {
                        LabeledContent("Худшая дистанция",
                                       value: String(format: "%.2f м", worst.minDistM))
                        LabeledContent("Точка", value: worst.point.map {
                            String(format: "x %.0f · y %.0f · z %.0f", $0[0], $0[1], $0[2])
                        } ?? "—")
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    private func briefCard(_ title: String, _ value: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title2.weight(.bold)).foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
    }

    // MARK: - Артефакты

    private var artifactsSection: some View {
        GroupBox("Ночные артефакты") {
            VStack(alignment: .leading, spacing: 10) {
                if cinematicURL != nil {
                    Button {
                        if let url = cinematicURL {
                            NSWorkspace.shared.open(url)
                        }
                    } label: {
                        Label("Кинематографичный ролик недели (Cycles)", systemImage: "film.fill")
                    }
                    .buttonStyle(.borderedProminent)
                }
                Button {
                    model.openReport()
                } label: {
                    Label("PDF-отчёт по площадке", systemImage: "doc.richtext")
                }
                Button {
                    model.selectedPage = .viewer3D
                } label: {
                    Label("Открыть 3D-вьюер", systemImage: "rotate.3d")
                }
                Button {
                    model.selectedPage = .plan
                } label: {
                    Label("План площадки", systemImage: "map")
                }
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var cinematicURL: URL? {
        let url = model.settings.outputURL.appendingPathComponent("simulation_cinematic.mp4")
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    private func loadHero() {
        let dir = model.settings.outputURL.appendingPathComponent("briefing")
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: [.contentModificationDateKey]) else { return }
        let latest = files
            .filter { $0.pathExtension == "png" && $0.lastPathComponent.hasPrefix("hero_") }
            .sorted { a, b in
                let da = (try? a.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate) ?? .distantPast
                let db = (try? b.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate) ?? .distantPast
                return da > db
            }
            .first
        if let latest {
            heroImage = NSImage(contentsOf: latest)
        }
    }
}

// MARK: - Модель дня для брифинга

struct BriefingDay {
    let dateLabel: String
    let liftCount: Int
    let findingCount: Int
    let criticalCount: Int
    let safeWindowCount: Int
    let topMessages: [String]
}

extension AppModel {
    /// Первый рабочий день таймлайна и его сводка для брифинга.
    var briefingDay: BriefingDay? {
        guard let bundle else { return nil }
        let frames = bundle.timeline.frames
        guard let firstT = frames.first?.t else { return nil }
        let dayPrefix = String(firstT.prefix(10))

        func day(_ s: String) -> Bool { s.hasPrefix(dayPrefix) }

        let dayFindings = bundle.findings.filter { day($0.start) }
        let dayLifts = bundle.lifts.filter { lift in
            lift.window?.first.map(day) ?? false
        }
        let safeWindows = bundle.lifts.compactMap { lift -> SafeWindow? in
            lift.summary.safeWindows?.first { $0.inTaskWindow && day($0.start) }
        }
        var messages: [String] = []
        let rank = ["critical": 0, "high": 1, "medium": 2, "low": 3]
        for f in dayFindings.sorted(by: {
            (rank[$0.severity] ?? 9) < (rank[$1.severity] ?? 9)
        }).prefix(3) {
            messages.append("\(f.ruleTitle): \(f.message)")
        }

        return BriefingDay(
            dateLabel: dayPrefix,
            liftCount: dayLifts.count,
            findingCount: dayFindings.count,
            criticalCount: dayFindings.filter { $0.severity == "critical" }.count,
            safeWindowCount: safeWindows.count,
            topMessages: messages
        )
    }
}
