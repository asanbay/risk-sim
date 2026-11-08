import SwiftUI

struct LiftDetailView: View {
    let lift: LiftAssessment
    let crane: Crane?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(crane?.id ?? "Кран")
                            .font(.title2.weight(.bold))
                        Text(lift.loadName)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        if let window = lift.window, window.count == 2 {
                            Text("\(window[0]) → \(window[1])")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    StatusBadge(utilization: lift.summary.craneUtilizationMax)
                }

                GroupBox("Показатели подъёма") {
                    Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 9) {
                        GridRow {
                            MetricLine(title: "Полная масса", value: "\(Int(lift.input.massKg * (lift.daf ?? 1))) кг")
                            MetricLine(title: "DAF", value: String(format: "%.2f", lift.daf ?? 1))
                        }
                        GridRow {
                            MetricLine(title: "Паспортная грузоподъёмность", value: capacityText)
                            MetricLine(title: "Использование", value: utilizationText)
                        }
                        GridRow {
                            MetricLine(title: "Длина стрелы", value: boomText)
                            MetricLine(title: "Вылет", value: String(format: "%.2f м", lift.input.radiusM))
                        }
                        GridRow {
                            MetricLine(title: "Высота крюка", value: String(format: "%.2f м", lift.input.hookHeightM))
                            MetricLine(title: "Предел ветра", value: String(format: "%.1f м/с", lift.input.windLimitMs))
                        }
                        GridRow {
                            MetricLine(title: "Натяжение ветви", value: tensionText)
                            MetricLine(title: "Ветер на высоте", value: String(format: "%.2f м/с", lift.summary.vWorkMaxMs))
                        }
                    }
                    .padding(8)
                }

                if let crane, !crane.loadChart.isEmpty {
                    GroupBox("Грузовая характеристика") {
                        LoadCurveView(points: crane.loadChart, current: lift)
                            .frame(height: 190)
                            .padding(8)
                        Text("Зелёная точка — паспортная грузоподъёмность на вылете, красная — расчётная нагрузка (масса × DAF).")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 8)
                            .padding(.bottom, 8)
                    }
                }

                if let violations = lift.violations, !violations.isEmpty {
                    GroupBox("Нарушения по подъёму: \(violations.count)") {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(violations) { violation in
                                VStack(alignment: .leading, spacing: 3) {
                                    HStack(spacing: 8) {
                        Text(violation.severity.uppercased())
                            .font(.caption2.weight(.bold))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(violationColor(violation.severity).opacity(0.18), in: Capsule())
                            .foregroundStyle(violationColor(violation.severity))
                                        Text(violation.title).font(.callout.weight(.semibold))
                                    }
                                    Text(violation.message)
                                        .font(.callout)
                                        .fixedSize(horizontal: false, vertical: true)
                                    if let source = violation.source {
                                        Text("Источник: \(source)")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                                Divider()
                            }
                        }
                        .padding(8)
                    }
                }

                if let windows = lift.summary.safeWindows, !windows.isEmpty {
                    GroupBox("Безопасные окна по ветру") {
                        Table(windows) {
                            TableColumn("Начало") { Text($0.start).font(.caption.monospacedDigit()) }
                            TableColumn("Конец") { Text($0.end).font(.caption.monospacedDigit()) }
                            TableColumn("Часы") { Text(String(format: "%.1f", $0.hours)).font(.caption.monospacedDigit()) }
                            TableColumn("Сдвиг от старта, ч") {
                                Text($0.shiftFromTaskStartH.map { String(format: "%+.1f", $0) } ?? "—")
                                    .font(.caption.monospacedDigit())
                            }
                        }
                        .tableStyle(.bordered)
                        .frame(height: min(CGFloat(windows.count) * 28 + 40, 160))
                        .padding(8)
                    }
                }

                if let swing = lift.swing, swing.movingAnchor == true {
                    GroupBox("Полная динамика MuJoCo") {
                        Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 8) {
                            GridRow {
                                MetricLine(title: "Контакты с конструкцией", value: "\(swing.structuralCollisionCount ?? swing.collisionCount ?? 0)")
                                MetricLine(title: "Скорость сближения", value: String(format: "%.2f м/с", swing.contactMaxImpactSpeedM ?? 0))
                            }
                            GridRow {
                                MetricLine(title: "Период подвеса", value: swing.naturalPeriodS.map { String(format: "%.2f с", $0) } ?? "—")
                                MetricLine(title: "Смещение", value: swing.steadyOffsetM.map { String(format: "%.2f м", $0) } ?? "—")
                            }
                        }
                        .padding(8)
                    }
                }

                GroupBox("Ограничения") {
                    Text("Контакт с землёй на складе — отдельная опора, а не столкновение. Численный контактный импульс не используется как инженерная сила. Допустимые значения подтверждает ответственный инженер по ППР и паспортам.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(8)
                }
            }
            .padding(18)
        }
    }

    private var capacityText: String {
        lift.summary.craneCapacityKgAtRadius.map { "\(Int($0)) кг" } ?? "—"
    }

    private func violationColor(_ severity: String) -> Color {
        switch severity {
        case "critical": return .red
        case "high": return .orange
        case "medium": return .yellow
        default: return .blue
        }
    }

    private var utilizationText: String {
        lift.summary.craneUtilizationMax.map { String(format: "%.1f%%", $0 * 100) } ?? "—"
    }

    private var boomText: String {
        crane.map { String(format: "%.2f м", $0.jib.lengthM) } ?? "—"
    }

    private var tensionText: String {
        lift.summary.slingTensionMaxN.map { "\(Int($0)) Н" } ?? "—"
    }
}

private struct StatusBadge: View {
    let utilization: Double?

    var body: some View {
        let ok = (utilization ?? 1) <= 0.9
        Text(ok ? "Норма" : "Проверить")
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background((ok ? Color.green : Color.red).opacity(0.15), in: Capsule())
            .foregroundStyle(ok ? Color.green : Color.red)
    }
}

private struct MetricLine: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.callout.weight(.medium))
        }
    }
}

private struct LoadCurveView: View {
    let points: [CraneLoadPoint]
    let current: LiftAssessment

    var body: some View {
        GeometryReader { geometry in
            let maxRadius = max(points.map(\.radiusM).max() ?? 1, 1)
            let maxLoad = max(points.map(\.maxLoadKg).max() ?? 1, 1)
            let width = geometry.size.width
            let height = geometry.size.height

            ZStack(alignment: .bottomLeading) {
                Path { path in
                    path.move(to: .zero)
                    path.addLine(to: CGPoint(x: width, y: 0))
                    path.move(to: .zero)
                    path.addLine(to: CGPoint(x: 0, y: height))
                }
                .stroke(.quaternary, lineWidth: 1)

                Path { path in
                    for (index, point) in points.enumerated() {
                        let x = point.radiusM / maxRadius * width
                        let y = height - point.maxLoadKg / maxLoad * height
                        if index == 0 {
                            path.move(to: CGPoint(x: x, y: y))
                        } else {
                            path.addLine(to: CGPoint(x: x, y: y))
                        }
                    }
                }
                .stroke(Color.yellow, style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))

                if let radius = current.summary.craneCapacityKgAtRadius {
                    Circle()
                        .fill(Color.green)
                        .frame(width: 9, height: 9)
                        .position(
                            x: current.input.radiusM / maxRadius * width,
                            y: height - radius / maxLoad * height
                        )
                    // Расчётная нагрузка: масса × DAF — насколько ниже кривой работаем.
                    let loadMass = current.input.massKg * (current.daf ?? 1)
                    Circle()
                        .fill(Color.red)
                        .frame(width: 9, height: 9)
                        .position(
                            x: current.input.radiusM / maxRadius * width,
                            y: height - min(loadMass, maxLoad) / maxLoad * height
                        )
                }
            }
            .overlay(alignment: .bottomLeading) {
                Text("Вылет, м").font(.caption2).foregroundStyle(.secondary)
            }
            .overlay(alignment: .topLeading) {
                Text("Нагрузка, кг").font(.caption2).foregroundStyle(.secondary)
            }
        }
    }
}
