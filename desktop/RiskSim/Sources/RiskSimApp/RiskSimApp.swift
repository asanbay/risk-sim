import SwiftUI

@main
struct RiskSimApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup("RiskSim — Цифровой двойник работ") {
            ContentView()
                .environmentObject(model)
        }
        .defaultSize(width: 1280, height: 800)
    }
}
