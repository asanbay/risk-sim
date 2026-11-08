import SwiftUI

/// Интерактивный вид площадки сверху: зоны работ, кран со стрелой и грузы
/// в текущий момент модельного времени.
struct PlanView: View {
    @EnvironmentObject private var model: AppModel
    @State private var level = "Все ярусы"

    private let levelAll = "Все ярусы"

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let timeline = model.bundle?.timeline {
                HStack(spacing: 14) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(model.currentFrame?.t ?? "—")
                            .font(.title3.weight(.semibold)).monospacedDigit()
                        if let tasks = model.currentFrame?.tasksActive, !tasks.isEmpty {
                            Text("Активные задачи: \(tasks.joined(separator: ", "))")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                    Spacer()
                    Picker("Ярус", selection: $level) {
                        Text(levelAll).tag(levelAll)
                        ForEach(availableLevels, id: \.self) { Text($0).tag($0) }
                    }
                    .pickerStyle(.menu)
                    .fixedSize()
                    .frame(width: 170, alignment: .trailing)
                }

                SiteCanvas(
                    zones: filteredZones(timeline.zones),
                    crane: timeline.crane,
                    frame: model.currentFrame
                )
                .aspectRatio(1.5, contentMode: .fit)
                .frame(maxWidth: .infinity)
                .background(Color(nsColor: .textBackgroundColor).opacity(0.6), in: RoundedRectangle(cornerRadius: 10))

                Slider(
                    value: $model.scrub,
                    in: 0...Double(max(model.frameCount - 1, 0)),
                    step: 1
                ) {
                    Text("Модельное время")
                }
                HStack {
                    PlaybackControls()
                    Spacer()
                    legend
                }
            } else {
                EmptyStateView("Нет площадки", systemImage: "map", description: Text("Запустите расчёт пилота"))
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var availableLevels: [String] {
        let levels = Set(model.bundle?.timeline.zones.map(\.level) ?? [])
        return levels.sorted()
    }

    private func filteredZones(_ zones: [Zone]) -> [Zone] {
        level == levelAll ? zones : zones.filter { $0.level == level }
    }

    private var legend: some View {
        HStack(spacing: 12) {
            ForEach(ZonePalette.legend, id: \.type) { item in
                HStack(spacing: 4) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(item.color.opacity(0.55))
                        .frame(width: 12, height: 12)
                    Text(item.title).font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}

/// Кнопки воспроизведения, общие для план-вида и таймлайна.
struct PlaybackControls: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(spacing: 10) {
            Button {
                model.togglePlayback()
            } label: {
                Image(systemName: model.isPlaying ? "pause.fill" : "play.fill")
            }
            .help(model.isPlaying ? "Пауза" : "Воспроизвести")
            .keyboardShortcut(.space, modifiers: [])

            Text("\(Int(model.scrub.rounded()) + 1)/\(max(model.frameCount, 1))")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)

            Picker("Скорость", selection: $model.playbackSpeed) {
                Text("×2").tag(2.0)
                Text("×4").tag(4.0)
                Text("×8").tag(8.0)
                Text("×16").tag(16.0)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 150)
        }
    }
}

// MARK: - Отрисовка площадки

private struct SiteCanvas: View {
    let zones: [Zone]
    let crane: Crane
    let frame: TimelineFrame?

    var body: some View {
        Canvas { context, size in
            let bounds = siteBounds
            let worldWidth = bounds.maxX - bounds.minX
            let worldHeight = bounds.maxY - bounds.minY
            guard worldWidth > 0, worldHeight > 0 else { return }
            let scale = min(size.width / worldWidth, size.height / worldHeight) * 0.92
            let origin = CGPoint(
                x: (size.width - worldWidth * scale) / 2,
                y: (size.height + worldHeight * scale) / 2
            )
            // Мировые координаты -> экранные: x вправо, y вверх.
            func point(_ x: Double, _ y: Double) -> CGPoint {
                CGPoint(x: origin.x + (x - bounds.minX) * scale,
                        y: origin.y - (y - bounds.minY) * scale)
            }

            for zone in zones {
                let a = point(zone.xMin, zone.yMin)
                let b = point(zone.xMax, zone.yMax)
                let rect = CGRect(x: a.x, y: b.y, width: b.x - a.x, height: a.y - b.y)
                let color = ZonePalette.color(for: zone.type)
                context.fill(Path(rect), with: .color(color.opacity(0.30)))
                context.stroke(Path(rect), with: .color(color.opacity(0.85)), lineWidth: 1)
                if rect.width > 34, rect.height > 15 {
                    context.draw(
                        Text(zone.id).font(.system(size: 9, weight: .semibold)).foregroundColor(color),
                        at: CGPoint(x: rect.midX, y: rect.midY)
                    )
                }
            }

            drawCrane(context: context, point: point, scale: scale)
            drawLoads(context: context, point: point)
        }
    }

    private var siteBounds: (minX: Double, minY: Double, maxX: Double, maxY: Double) {
        var minX = 0.0, minY = 0.0, maxX = 1.0, maxY = 1.0
        var first = true
        func expand(_ x1: Double, _ y1: Double, _ x2: Double, _ y2: Double) {
            minX = first ? x1 : min(minX, x1); minY = first ? y1 : min(minY, y1)
            maxX = first ? x2 : max(maxX, x2); maxY = first ? y2 : max(maxY, y2)
            first = false
        }
        for zone in zones { expand(zone.xMin, zone.yMin, zone.xMax, zone.yMax) }
        if let mast = crane.mast {
            let r = crane.jib.lengthM
            expand(mast.x - r, mast.y - r, mast.x + r, mast.y + r)
        }
        for load in frame?.loads ?? [] {
            expand(load.x - 3, load.y - 3, load.x + 3, load.y + 3)
        }
        return (minX - 4, minY - 4, maxX + 4, maxY + 4)
    }

    private func drawCrane(context: GraphicsContext, point: (Double, Double) -> CGPoint, scale: Double) {
        guard let state = frame?.crane else {
            // Кран есть, но в этот момент не движется — рисуем только основание.
            if let mast = crane.mast {
                let p = point(mast.x, mast.y)
                context.fill(Path(ellipseIn: CGRect(x: p.x - 5, y: p.y - 5, width: 10, height: 10)),
                             with: .color(.secondary.opacity(0.7)))
            }
            return
        }
        let mastX = state.mastX ?? crane.mast?.x ?? 0
        let mastY = state.mastY ?? crane.mast?.y ?? 0
        let mast = point(mastX, mastY)
        let angle = Angle.degrees(state.slewDeg)

        // Дуга reach-области радиусом стрелы.
        if crane.jib.lengthM > 0 {
            let r = crane.jib.lengthM * scale
            let arc = Path { path in
                path.addEllipse(in: CGRect(x: mast.x - r, y: mast.y - r, width: 2 * r, height: 2 * r))
            }
            context.stroke(arc, with: .color(.secondary.opacity(0.25)),
                           style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
        }

        // Противовесная консоль.
        if let counter = crane.jib.counterJibM, counter > 0 {
            let tip = CGPoint(
                x: mast.x - cos(angle.radians) * counter * scale,
                y: mast.y + sin(angle.radians) * counter * scale
            )
            var path = Path()
            path.move(to: mast)
            path.addLine(to: tip)
            context.stroke(path, with: .color(.gray.opacity(0.8)), lineWidth: 4)
        }

        // Стрела и тележка.
        let jibTip = CGPoint(
            x: mast.x + cos(angle.radians) * crane.jib.lengthM * scale,
            y: mast.y - sin(angle.radians) * crane.jib.lengthM * scale
        )
        var jib = Path()
        jib.move(to: mast)
        jib.addLine(to: jibTip)
        context.stroke(jib, with: .color(.orange), style: StrokeStyle(lineWidth: 4, lineCap: .round))

        let trolley = CGPoint(
            x: mast.x + cos(angle.radians) * state.radiusM * scale,
            y: mast.y - sin(angle.radians) * state.radiusM * scale
        )
        context.fill(Path(ellipseIn: CGRect(x: trolley.x - 4, y: trolley.y - 4, width: 8, height: 8)),
                     with: .color(.orange))

        // Башня.
        context.fill(Path(ellipseIn: CGRect(x: mast.x - 6, y: mast.y - 6, width: 12, height: 12)),
                     with: .color(.black.opacity(0.75)))
    }

    private func drawLoads(context: GraphicsContext, point: (Double, Double) -> CGPoint) {
        for load in frame?.loads ?? [] {
            let p = point(load.x, load.y)
            let active = load.phase != "done"
            let radius: Double = active ? 7 : 4
            let color: Color = active ? .red : .secondary
            context.fill(Path(ellipseIn: CGRect(x: p.x - radius, y: p.y - radius, width: radius * 2, height: radius * 2)),
                         with: .color(color))
            if active {
                context.draw(
                    Text(load.liftId).font(.system(size: 9, weight: .bold)).foregroundColor(.red),
                    at: CGPoint(x: p.x, y: p.y - 12)
                )
            }
        }
    }
}

// MARK: - Палитра зон

enum ZonePalette {
    struct LegendItem {
        let type: String
        let title: String
        let color: Color
    }

    static let legend: [LegendItem] = [
        LegendItem(type: "work", title: "Работы", color: .blue),
        LegendItem(type: "passage", title: "Проход", color: .green),
        LegendItem(type: "storage", title: "Склад", color: .brown),
        LegendItem(type: "edge", title: "Край", color: .purple),
        LegendItem(type: "crane_hazard", title: "Зона крана", color: .red),
    ]

    static func color(for type: String) -> Color {
        switch type {
        case "work": return .blue
        case "passage": return .green
        case "storage": return .brown
        case "edge": return .purple
        case "crane_hazard": return .red
        default: return .gray
        }
    }
}
