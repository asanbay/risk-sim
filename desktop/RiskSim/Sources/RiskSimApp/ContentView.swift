import SwiftUI

/// Цвет плашки критичности; общий для списка конфликтов и деталей подъёма.
func severityColor(_ severity: String) -> Color {
    switch severity {
    case "critical": return .red
    case "high": return .orange
    case "medium": return .yellow
    default: return .blue
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationSplitView {
            List(AppPage.allCases) { page in
                Button {
                    model.selectedPage = page
                } label: {
                    Label(page.rawValue, systemImage: page.icon)
                        .foregroundStyle(model.selectedPage == page ? Color.accentColor : Color.primary)
                }
                .buttonStyle(.plain)
                .padding(.vertical, 4)
            }
            .navigationTitle("RiskSim")
            .frame(minWidth: 190)
        } detail: {
            VStack(spacing: 0) {
                HeaderView()
                Divider()
                Group {
                    switch model.selectedPage {
                    case .briefing: BriefingView()
                    case .overview: OverviewView()
                    case .viewer3D: Viewer3DPage()
                    case .plan: PlanView()
                    case .conflicts: ConflictsView()
                    case .lifts: LiftsView()
                    case .timeline: TimelineView()
                    case .reports: ReportsView()
                    case .settings: SettingsView()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                Divider()
                LogView()
            }
        }
        .frame(minWidth: 1100, minHeight: 720)
        .alert(
            "Ошибка",
            isPresented: Binding(
                get: { model.errorMessage != nil },
                set: { _ in model.errorMessage = nil }
            ),
            actions: { Button("OK") { model.errorMessage = nil } },
            message: { Text(model.errorMessage ?? "") }
        )
    }
}

private struct HeaderView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(model.projectTitle)
                    .font(.title2.weight(.semibold))
                    .lineLimit(1)
                Text(settingsSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            if model.isBusy {
                ProgressView().controlSize(.small)
                Button("Отменить", action: model.cancelRun)
                    .tint(.red)
            } else {
                Menu {
                    Button("Полный расчёт (MuJoCo + Monte Carlo + GIF)", action: model.runPilot)
                        .keyboardShortcut("r", modifiers: .command)
                    Button("Быстрый расчёт (грубая оценка)", action: model.runPilotFast)
                    Divider()
                    Button("Импорт IFC…", action: model.chooseIFC)
                } label: {
                    Label("Расчёт", systemImage: "play.fill")
                }
                .buttonStyle(.borderedProminent)
                .fixedSize()
                .disabled(!model.pythonReady)
                .help(model.pythonReady ? "Варианты расчёта" : "Python-окружение не найдено — проверьте настройки")
            }
            Button("Blender", action: model.openBlender)
        }
        .padding(14)
    }

    private var settingsSummary: String {
        let model = self.model
        return "\(model.liftCount) подъёмов · \(model.criticalCount) критических · \(model.frameCount) кадров"
    }
}

private struct OverviewView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(spacing: 12) {
                    MetricCard(title: "События", value: "\(model.bundle?.findings.count ?? 0)", color: .orange)
                    MetricCard(title: "Критические", value: "\(model.criticalCount)", color: .red)
                    MetricCard(title: "Подъёмы", value: "\(model.liftCount)", color: .blue)
                    MetricCard(title: "Кадры", value: "\(model.frameCount)", color: .green)
                }
                if let bySeverity = model.bundle?.findingsSummary?.bySeverity, !bySeverity.isEmpty {
                    GroupBox("События по критичности") {
                        HStack(spacing: 10) {
                            ForEach(["critical", "high", "medium"], id: \.self) { key in
                                if let count = bySeverity[key] {
                                    SeverityChip(severity: key, count: count)
                                }
                            }
                        }
                        .padding(8)
                    }
                }
                if let mc = model.bundle?.monteCarlo {
                    MonteCarloSection(mc: mc)
                }
                if let clr = model.bundle?.clearances {
                    ClearanceSection(clr: clr)
                }
                if let agents = model.bundle?.agents?.summary {
                    AgentsSection(summary: agents)
                }
                if let modelInfo = model.bundle?.model {
                    GroupBox("IFC-модель") {
                        VStack(alignment: .leading, spacing: 6) {
                            LabeledContent("Источник", value: modelInfo.sourceFile)
                            LabeledContent("Элементы", value: "\(modelInfo.elementCount)")
                            LabeledContent("Треугольники", value: "\(modelInfo.triangleCount)")
                            LabeledContent("Ошибки геометрии", value: "\(modelInfo.failedElementCount)")
                        }
                        .padding(8)
                    }
                } else {
                    Label("Процедурная демонстрационная оболочка. Для реального объекта импортируйте IFC и ППР.", systemImage: "info.circle")
                        .foregroundStyle(.secondary)
                }
                if !model.dataWarnings.isEmpty {
                    GroupBox("Предупреждения о качестве данных") {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(model.dataWarnings, id: \.self) { warning in
                                Label(warning, systemImage: "exclamationmark.triangle.fill")
                                    .foregroundStyle(.orange)
                                    .font(.callout)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(8)
                    }
                }
                GroupBox("Границы применения") {
                    Text("Результат симуляции — поддержка решений, а не расчёт по нормам. Не заменяет ППР, паспорта крана и строп, нормы и ответственного инженера.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(8)
                }
            }
            .padding(18)
        }
    }
}

private struct SeverityChip: View {
    let severity: String
    let count: Int

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(title).font(.callout.weight(.medium))
            Text("\(count)").font(.callout.monospacedDigit().bold())
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(color.opacity(0.12), in: Capsule())
    }

    private var color: Color {
        switch severity {
        case "critical": return .red
        case "high": return .orange
        case "medium": return .yellow
        default: return .blue
        }
    }

    private var title: String {
        switch severity {
        case "critical": return "Критические"
        case "high": return "Высокие"
        case "medium": return "Средние"
        default: return severity
        }
    }
}

/// Вероятности из Monte Carlo с дружелюбными названиями метрик.
enum MCLabels {
    static func liftTitle(_ key: String) -> String {
        switch key {
        case "P_any_critical": return "Любое критическое событие"
        case "P_wind_above_ppr_limit": return "Ветер выше предела ППР"
        case "P_wind_above_crane_limit": return "Ветер выше предела крана"
        case "P_tagline_force_exceeded": return "Оттяжки: сила превышена"
        case "P_sling_utilization_exceeded": return "Стропы: использование превышено"
        case "P_crane_chart_exceeded": return "Грузовая характеристика превышена"
        case "P_load_offset_over_1m": return "Снос груза > 1 м"
        case "P_load_offset_over_3m": return "Снос груза > 3 м"
        default: return key
        }
    }

    static func scheduleTitle(_ key: String) -> String {
        switch key {
        case "no_two_crews_same_zone": return "Две бригады в одной зоне"
        case "no_work_below_active_work": return "Работы одна над другой"
        default: return key
        }
    }
}

private struct MonteCarloSection: View {
    let mc: MonteCarloBlock

    var body: some View {
        GroupBox("Monte Carlo — вероятность нарушений по подъёмам") {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(mc.lifts) { lift in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Text(lift.liftId).font(.callout.monospaced().weight(.semibold))
                            Text("n=\(lift.n) · \(lift.method)").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            if let p = lift.probabilities["P_any_critical"] {
                                ProbabilityBadge(value: p)
                            }
                        }
                        HStack(spacing: 14) {
                            ForEach(probabilityRows(lift), id: \.0) { key, value in
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(MCLabels.liftTitle(key)).font(.caption2).foregroundStyle(.secondary)
                                    Text(String(format: "%.0f%%", value * 100))
                                        .font(.caption.monospacedDigit().weight(.medium))
                                        .foregroundStyle(value >= 0.5 ? Color.red : Color.primary)
                                }
                            }
                        }
                    }
                    .padding(.vertical, 3)
                    if lift.id != mc.lifts.last?.id { Divider() }
                }
                if let schedule = mc.schedule {
                    Divider()
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Расписание").font(.callout.weight(.semibold))
                        HStack(spacing: 14) {
                            ForEach(schedule.ruleProbabilities.sorted(by: { $0.value > $1.value }), id: \.key) { key, value in
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(MCLabels.scheduleTitle(key)).font(.caption2).foregroundStyle(.secondary)
                                    Text(String(format: "%.0f%%", value * 100))
                                        .font(.caption.monospacedDigit().weight(.medium))
                                        .foregroundStyle(value >= 0.5 ? Color.red : Color.primary)
                                }
                            }
                        }
                        if let exposure = schedule.exposurePersonHours {
                            Text("Облучение людей: среднее \(String(format: "%.0f", exposure["mean"] ?? 0)) чел·ч · p95 \(String(format: "%.0f", exposure["p95"] ?? 0)) чел·ч · максимум \(String(format: "%.0f", exposure["max"] ?? 0)) чел·ч")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .padding(8)
        }
    }

    private func probabilityRows(_ lift: MCLift) -> [(String, Double)] {
        let order = ["P_wind_above_ppr_limit", "P_wind_above_crane_limit", "P_tagline_force_exceeded",
                     "P_sling_utilization_exceeded", "P_crane_chart_exceeded",
                     "P_load_offset_over_1m", "P_load_offset_over_3m"]
        return order.compactMap { key in lift.probabilities[key].map { (key, $0) } }
    }
}

private struct ProbabilityBadge: View {
    let value: Double

    var body: some View {
        let high = value >= 0.5
        Text(String(format: "P(крит) %.0f%%", value * 100))
            .font(.caption.weight(.semibold).monospacedDigit())
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background((high ? Color.red : Color.green).opacity(0.15), in: Capsule())
            .foregroundStyle(high ? .red : .green)
    }
}

private struct AgentsSection: View {
    let summary: AgentsSummary

    var body: some View {
        GroupBox("Агенты бригад (микросимуляция)") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 14) {
                    LabeledContent("События", value: "\(summary.nEvents ?? 0)")
                    LabeledContent("Человеко-минут", value: String(format: "%.0f", summary.totalPersonMinutes ?? 0))
                    LabeledContent("Шаг dt", value: String(format: "%.0f мин", summary.dtMin ?? 0))
                }
                .font(.callout)
                ForEach(summary.byRule.sorted(by: { $0.value.personMinutes > $1.value.personMinutes }), id: \.key) { key, stat in
                    HStack {
                        Text(key).font(.caption.weight(.medium))
                        Spacer()
                        Text("\(stat.events) событий · \(Int(stat.personMinutes)) чел·мин · до \(stat.peopleMax) чел.")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(8)
        }
    }
}

private struct ClearanceSection: View {
    let clr: ClearancesBlock

    var body: some View {
        GroupBox("Клиренс грузового пути (3D-модель)") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 14) {
                    LabeledContent("Допуск", value: String(format: "%.2f м", clr.marginM))
                    LabeledContent("События", value: "\(clr.events.count)")
                    if let worst = clr.worst {
                        LabeledContent("Худшая дистанция", value: String(format: "%.2f м", worst.minDistM))
                    }
                }
                .font(.callout)
                if let model = clr.modelFile {
                    Text("Модель: \(model) · индекс: \(clr.indexMode ?? "—") · кадров: \(clr.checkedFrames ?? 0)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                ForEach(clr.events.prefix(6)) { ev in
                    HStack(alignment: .top, spacing: 8) {
                        Circle()
                            .fill(ev.status == "critical" ? Color.red : Color.orange)
                            .frame(width: 8, height: 8)
                            .padding(.top, 4)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(ev.liftId ?? "—"): мин. \(String(format: "%.2f м", ev.minDistM)) при допуске \(String(format: "%.2f м", ev.marginM)) (\(ev.component ?? "—"))")
                                .font(.caption.weight(.medium))
                            Text("\(ev.start) → \(ev.end)")
                                .font(.caption2.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                if clr.events.count > 6 {
                    Text("… и ещё \(clr.events.count - 6) в списке конфликтов")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(8)
        }
    }
}

private struct MetricCard: View {
    let title: String
    let value: String
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title.weight(.bold)).foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct ConflictsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var severityFilter = "all"
    @State private var searchText = ""

    private struct FilterOption: Identifiable {
        let id: String
        let title: String
    }

    private let filters: [FilterOption] = [
        FilterOption(id: "all", title: "Все"),
        FilterOption(id: "critical", title: "Критические"),
        FilterOption(id: "high", title: "Высокие"),
        FilterOption(id: "medium", title: "Средние"),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let bundle = model.bundle {
                VStack(alignment: .leading, spacing: 8) {
                    Picker("Критичность", selection: $severityFilter) {
                        ForEach(filters) { Text($0.title).tag($0.id) }
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 420)

                    if let rules = bundle.findingsSummary?.byRule, !rules.isEmpty {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(rules.sorted { $0.n > $1.n }) { rule in
                                    Button {
                                        searchText = rule.ruleTitle
                                    } label: {
                                        HStack(spacing: 5) {
                                            Circle()
                                                .fill(severityColor(rule.severity))
                                                .frame(width: 7, height: 7)
                                            Text("\(rule.ruleTitle) · \(rule.n)")
                                                .font(.caption)
                                        }
                                        .padding(.horizontal, 8)
                                        .padding(.vertical, 4)
                                        .background(.quaternary.opacity(0.4), in: Capsule())
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 4)

                List(filteredFindings) { finding in
                    FindingRow(finding: finding)
                }
                .listStyle(.inset)
                .searchable(text: $searchText, prompt: "Поиск по тексту или правилу")
            } else {
                EmptyStateView("Нет результатов", systemImage: "play", description: Text("Запустите расчёт пилота"))
            }
        }
    }

    private var filteredFindings: [Finding] {
        let findings = model.bundle?.findings ?? []
        var result = severityFilter == "all" ? findings : findings.filter { $0.severity == severityFilter }
        if !searchText.isEmpty {
            let query = searchText.lowercased()
            result = result.filter {
                $0.message.lowercased().contains(query)
                    || $0.ruleTitle.lowercased().contains(query)
                    || $0.ruleId.lowercased().contains(query)
            }
        }
        return result
    }
}

private struct FindingRow: View {
    let finding: Finding

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(finding.severity.uppercased())
                    .font(.caption.weight(.bold))
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(severityColor.opacity(0.18), in: Capsule())
                    .foregroundStyle(severityColor)
                Text(finding.ruleTitle).font(.headline)
            }
            Text(finding.message)
                .fixedSize(horizontal: false, vertical: true)
            Text("\(finding.start) → \(finding.end) · \(finding.zoneIds.joined(separator: ", "))")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("Источник: \(finding.source)")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 6)
    }

    private var severityColor: Color {
        switch finding.severity {
        case "critical": return .red
        case "high": return .orange
        case "medium": return .yellow
        default: return .blue
        }
    }
}

private struct LiftsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var selectedId: String?

    var body: some View {
        NavigationSplitView {
            List(model.bundle?.lifts ?? []) { lift in
                Button {
                    selectedId = lift.liftId
                } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(lift.liftId).font(.headline.monospaced())
                            Text(lift.loadName).font(.caption).lineLimit(1)
                        }
                        Spacer()
                        Text("\(Int(lift.input.massKg)) кг").foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                }
                .buttonStyle(.plain)
                .background(selectedId == lift.liftId ? Color.accentColor.opacity(0.12) : Color.clear)
            }
            .navigationTitle("Подъёмы")
            .frame(minWidth: 290)
        } detail: {
            if let lift = selectedLift {
                LiftDetailView(lift: lift, crane: model.bundle?.timeline.crane)
            } else {
                EmptyStateView("Выберите подъём", systemImage: "crane", description: Text("Слева список операций из ППР"))
            }
        }
    }

    private var selectedLift: LiftAssessment? {
        if let selectedId {
            return model.bundle?.lifts.first(where: { $0.liftId == selectedId })
        }
        return model.bundle?.lifts.first
    }
}

private struct TimelineView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let frame = model.currentFrame {
                    HStack {
                        Text(frame.t)
                            .font(.title3.weight(.semibold)).monospacedDigit()
                        Spacer()
                        PlaybackControls()
                    }
                    Slider(
                        value: $model.scrub,
                        in: 0...Double(max(model.frameCount - 1, 0)),
                        step: 1
                    ) {
                        Text("Модельное время")
                    } minimumValueLabel: {
                        Text("0")
                    } maximumValueLabel: {
                        Text("\(max(model.frameCount - 1, 0))")
                    }
                    if let tasks = frame.tasksActive, !tasks.isEmpty {
                        GroupBox("Активные задачи: \(tasks.count)") {
                            FlowLayoutCompat(items: tasks)
                                .padding(8)
                        }
                    }
                    if let wind = frame.wind {
                        HStack(spacing: 12) {
                            MetricCard(title: "Порыв на 10 м", value: String(format: "%.2f м/с", wind.gustAtRefMs), color: .blue)
                            MetricCard(title: "Порыв на высоте", value: String(format: "%.2f м/с", wind.gustAtTopMs), color: .orange)
                            MetricCard(title: "Средняя на 10 м", value: String(format: "%.2f м/с", wind.meanAtRefMs), color: .green)
                        }
                    }
                    if let crane = frame.crane {
                        GroupBox("Кран") {
                            HStack(spacing: 24) {
                                LabeledContent("Поворот", value: String(format: "%.1f°", crane.slewDeg))
                                LabeledContent("Вылет", value: String(format: "%.1f м", crane.radiusM))
                                LabeledContent("Высота крюка", value: String(format: "%.1f м", crane.hookZM))
                                if let capacity = crane.capacityKg {
                                    LabeledContent("Г/п на вылете", value: "\(Int(capacity)) кг")
                                }
                            }
                            .padding(8)
                        }
                    }
                    GroupBox("Активные грузы: \(frame.loads.count)") {
                        if frame.loads.isEmpty {
                            Text("В этот момент подъёмов нет").foregroundStyle(.secondary).padding(8)
                        } else {
                            ForEach(frame.loads) { load in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("\(load.liftId) · \(load.loadName ?? "Груз")").font(.headline)
                                    Text("Фаза: \(load.phase) · масса: \(Int(load.massKg ?? 0)) кг · отклонение: \(String(format: "%.2f°", load.swingThetaDeg ?? 0))")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                    Text("x \(format(load.x)), y \(format(load.y)), z \(format(load.z))")
                                        .font(.caption.monospaced())
                                }
                                .padding(.vertical, 4)
                            }
                        }
                    }
                    Button("Открыть сцену в Blender", action: model.openBlender)
                } else {
                    EmptyStateView("Нет таймлайна", systemImage: "clock", description: Text("Запустите расчёт пилота"))
                }
            }
            .padding(18)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func format(_ value: Double) -> String { String(format: "%.2f", value) }
}

/// Компактные «чипы» для списка идентификаторов задач.
private struct FlowLayoutCompat: View {
    let items: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(chunked(), id: \.self) { row in
                HStack(spacing: 6) {
                    ForEach(row, id: \.self) { item in
                        Text(item)
                            .font(.caption.monospaced())
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(Color.accentColor.opacity(0.12), in: Capsule())
                    }
                }
            }
        }
    }

    private func chunked() -> [[String]] {
        var rows: [[String]] = []
        var current: [String] = []
        for item in items {
            current.append(item)
            if current.count == 4 {
                rows.append(current)
                current = []
            }
        }
        if !current.isEmpty { rows.append(current) }
        return rows
    }
}

private struct ReportsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Артефакты проекта").font(.title3.weight(.semibold))
            ReportButton(title: "Реалистичное видео (Blender)",
                         subtitle: "Фотореалистичный рендер стройки, simulation_realistic.mp4",
                         action: model.renderRealisticVideo, icon: "video.fill")
                .disabled(model.isBusy || !model.pythonReady)
            ReportButton(title: "Открыть ролик", subtitle: settingsPath("simulation_realistic.mp4"),
                         action: model.openRealisticVideo, icon: "play.rectangle")
            ReportButton(title: "PDF-отчёт", subtitle: settingsPath("report.pdf"), action: model.openReport, icon: "doc.richtext")
            ReportButton(title: "Word-отчёт", subtitle: settingsPath("report.docx"), action: model.openDOCX, icon: "doc.text")
            ReportButton(title: "Blender-сцена", subtitle: "Открыть crane_scene.blend с анимацией",
                         action: model.openBlender, icon: "cube")
            ReportButton(title: "OpenUSD", subtitle: "stage_flattened.usda", action: model.openUSD, icon: "square.stack.3d.up")
            ReportButton(title: "Каталог результатов", subtitle: model.settings.outputURL.path, action: model.openOutputFolder, icon: "folder")
            Button("Проверить физику", action: model.validatePhysics)
                .disabled(model.isBusy)
            Spacer()
        }
        .padding(18)
    }

    private func settingsPath(_ name: String) -> String { model.settings.outputURL.appendingPathComponent(name).path }
}

private struct ReportButton: View {
    let title: String
    let subtitle: String
    let action: () -> Void
    let icon: String

    var body: some View {
        Button(action: action) {
            HStack {
                Image(systemName: icon).frame(width: 28)
                VStack(alignment: .leading) {
                    Text(title)
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                Image(systemName: "chevron.right").foregroundStyle(.tertiary)
            }
            .padding(12)
            .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
    }
}

private struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            Section("Проект") {
                HStack {
                    TextField("Корень проекта", text: $model.settings.projectPath)
                        .onChange(of: model.settings.projectPath) { _ in
                            model.settingsEdited(reloadBundle: true)
                        }
                    Button("Выбрать…", action: model.chooseProjectRoot)
                }
                HStack {
                    TextField("Данные", text: $model.settings.dataPath)
                        .onChange(of: model.settings.dataPath) { _ in
                            model.settingsEdited(reloadBundle: true)
                        }
                    Button("Выбрать…", action: model.chooseDataDirectory)
                }
                HStack {
                    TextField("Результаты", text: $model.settings.outputPath)
                        .onChange(of: model.settings.outputPath) { _ in
                            model.settingsEdited(reloadBundle: true)
                        }
                    Button("Выбрать…", action: model.chooseOutputDirectory)
                }
            }
            Section("Blender") {
                HStack {
                    TextField("Исполняемый файл", text: $model.settings.blenderPath)
                        .onChange(of: model.settings.blenderPath) { _ in
                            model.settingsEdited(reloadBundle: false)
                        }
                    Button("Выбрать…", action: model.chooseBlenderBinary)
                }
            }
            Section("Окружение") {
                if model.pythonReady {
                    Label("Python-окружение найдено", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                } else {
                    Label("Python не найден: \(model.settings.pythonURL.path) — расчёты недоступны",
                          systemImage: "xmark.octagon.fill")
                        .foregroundStyle(.red)
                }
                Text("Python: \(model.settings.pythonURL.path)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            Section("Действия") {
                HStack {
                    Button("Обновить JSON", action: model.refresh)
                    Button("Сбросить настройки") {
                        model.settings = .default()
                        model.settingsEdited(reloadBundle: true)
                    }
                }
            }
        }
        .padding(18)
        .frame(maxWidth: 720, alignment: .leading)
    }
}

private struct LogView: View {
    @EnvironmentObject private var model: AppModel
    @State private var isExpanded = true

    var body: some View {
        DisclosureGroup("Журнал выполнения", isExpanded: $isExpanded) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(model.log.enumerated()), id: \.offset) { index, line in
                            Text(line)
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(index)
                        }
                    }
                    .padding(8)
                }
                .frame(height: 130)
                .onChange(of: model.log.count) { _ in
                    guard let last = model.log.indices.last else { return }
                    withAnimation(.easeOut(duration: 0.15)) {
                        proxy.scrollTo(last, anchor: .bottom)
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }
}

struct EmptyStateView: View {
    let title: String
    let systemImage: String
    let description: Text

    init(_ title: String, systemImage: String, description: Text) {
        self.title = title
        self.systemImage = systemImage
        self.description = description
    }

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: systemImage)
                .font(.largeTitle)
                .foregroundStyle(.secondary)
            Text(title).font(.headline)
            description.foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
