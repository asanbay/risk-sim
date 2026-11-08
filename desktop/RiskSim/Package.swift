// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "RiskSim",
    platforms: [.macOS(.v15)],
    products: [.executable(name: "RiskSimApp", targets: ["RiskSimApp"])],
    targets: [
        .executableTarget(name: "RiskSimApp", path: "Sources/RiskSimApp"),
        .testTarget(name: "RiskSimAppTests", dependencies: ["RiskSimApp"], path: "Tests/RiskSimAppTests")
    ]
)
