import Foundation

struct AppSettings: Codable {
    var projectPath: String
    var dataPath: String
    var outputPath: String
    var blenderPath: String

    static func `default`() -> AppSettings {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { root.deleteLastPathComponent() }
        if !FileManager.default.fileExists(atPath: root.appendingPathComponent("risk_sim/cli.py").path) {
            root = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("risk-sim")
        }
        let steamBlender = "\(NSHomeDirectory())/Library/Application Support/Steam/steamapps/common/Blender/Blender.app/Contents/MacOS/Blender"
        let standardBlender = "/Applications/Blender.app/Contents/MacOS/Blender"
        let blender = FileManager.default.fileExists(atPath: steamBlender) ? steamBlender : standardBlender
        return AppSettings(
            projectPath: root.path,
            dataPath: "data/sample",
            outputPath: "results/dynamics",
            blenderPath: blender
        )
    }

    static func load() -> AppSettings {
        guard let data = UserDefaults.standard.data(forKey: "RiskSim.Settings"),
              var settings = try? JSONDecoder().decode(AppSettings.self, from: data) else {
            return .default()
        }
        if settings.outputPath == "results/pilot" {
            settings.outputPath = "results/dynamics"
            settings.save()
        }
        return settings
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            UserDefaults.standard.set(data, forKey: "RiskSim.Settings")
        }
    }

    var projectURL: URL { URL(fileURLWithPath: projectPath, isDirectory: true) }
    var dataURL: URL { AppSettings.resolve(project: projectURL, path: dataPath) }
    var outputURL: URL { AppSettings.resolve(project: projectURL, path: outputPath) }
    var bundleURL: URL { outputURL.appendingPathComponent("scene_bundle.json") }
    var reportURL: URL { outputURL.appendingPathComponent("report.pdf") }
    var pythonURL: URL { projectURL.appendingPathComponent(".venv/bin/python") }

    static func resolve(project: URL, path: String) -> URL {
        path.hasPrefix("/") ? URL(fileURLWithPath: path) : project.appendingPathComponent(path)
    }
}

struct SceneBundle: Decodable {
    let timeline: Timeline
    let findings: [Finding]
    let lifts: [LiftAssessment]
    let context: ContextInfo?
    let model: ModelInfo?
    let findingsSummary: FindingsSummary?
    let monteCarlo: MonteCarloBlock?
    let agents: AgentsBlock?
    let clearances: ClearancesBlock?

    enum CodingKeys: String, CodingKey {
        case timeline, findings, lifts, context, model, agents, clearances
        case findingsSummary = "findings_summary"
        case monteCarlo = "monte_carlo"
    }
}

struct ClearancesBlock: Decodable {
    let marginM: Double
    let modelFile: String?
    let indexMode: String?
    let checkedFrames: Int?
    let worst: ClearanceFrame?
    let events: [ClearanceEvent]
    let perFrame: [ClearanceFrame]

    enum CodingKeys: String, CodingKey {
        case worst, events
        case marginM = "margin_m"
        case modelFile = "model_file"
        case indexMode = "index_mode"
        case checkedFrames = "checked_frames"
        case perFrame = "per_frame"
    }
}

struct AgentTrack: Decodable {
    let crewId: String
    let points: [AgentTrackPoint]

    enum CodingKeys: String, CodingKey {
        case crewId = "crew_id"
        case points
    }
}

struct AgentTrackPoint: Decodable {
    let t: String
    let x: Double
    let y: Double
    let z: Double

    enum CodingKeys: String, CodingKey {
        case t, x, y, z
    }
}

struct ClearanceFrame: Decodable {
    let minDistM: Double
    let component: String?
    let point: [Double]?
    let status: String?

    enum CodingKeys: String, CodingKey {
        case component, point, status
        case minDistM = "min_dist_m"
    }
}

struct ClearanceEvent: Decodable, Identifiable {
    let start: String
    let end: String
    let liftId: String?
    let minDistM: Double
    let marginM: Double
    let component: String?
    let status: String

    var id: String { "\(start)|\(liftId ?? "?")" }

    enum CodingKeys: String, CodingKey {
        case start, end, component, status
        case liftId = "lift_id"
        case minDistM = "min_dist_m"
        case marginM = "margin_m"
    }
}

struct FindingsSummary: Decodable {
    let total: Int
    let bySeverity: [String: Int]
    let byRule: [RuleCount]

    enum CodingKeys: String, CodingKey {
        case total
        case bySeverity = "by_severity"
        case byRule = "by_rule"
    }
}

struct RuleCount: Decodable, Identifiable {
    let ruleId: String
    let ruleTitle: String
    let severity: String
    let source: String
    let n: Int
    let people: Int?
    let personMin: Double?

    var id: String { ruleId }

    enum CodingKeys: String, CodingKey {
        case ruleId = "rule_id"
        case ruleTitle = "rule_title"
        case severity, source, n, people
        case personMin = "person_min"
    }
}

struct MonteCarloBlock: Decodable {
    let lifts: [MCLift]
    let schedule: MCSchedule?
}

struct MCLift: Decodable, Identifiable {
    let liftId: String
    let n: Int
    let method: String
    let probabilities: [String: Double]
    let summary: [String: MCStat]?

    var id: String { liftId }

    enum CodingKeys: String, CodingKey {
        case liftId = "lift_id"
        case n, method, probabilities, summary
    }
}

struct MCStat: Decodable {
    let mean: Double?
    let p50: Double?
    let p95: Double?
    let max: Double?
}

struct MCSchedule: Decodable {
    let n: Int?
    let ruleProbabilities: [String: Double]
    let exposurePersonHours: [String: Double]?
    let topPairs: [MCTopPair]?

    enum CodingKeys: String, CodingKey {
        case n
        case ruleProbabilities = "rule_probabilities"
        case exposurePersonHours = "exposure_person_hours"
        case topPairs = "top_pairs"
    }
}

struct MCTopPair: Decodable, Identifiable {
    let taskA: String
    let taskB: String
    let zoneA: String
    let zoneB: String
    let crewA: String
    let crewB: String
    let people: Int
    let pAny: Double?

    var id: String { "\(taskA)+\(taskB)" }

    enum CodingKeys: String, CodingKey {
        case taskA = "task_a"
        case taskB = "task_b"
        case zoneA = "zone_a"
        case zoneB = "zone_b"
        case crewA = "crew_a"
        case crewB = "crew_b"
        case people
        case pAny = "P_any"
    }
}

struct AgentsBlock: Decodable {
    let summary: AgentsSummary?
    let tracks: [AgentTrack]?
}

struct AgentsSummary: Decodable {
    let steps: Int?
    let dtMin: Double?
    let nEvents: Int?
    let totalPersonMinutes: Double?
    let byRule: [String: AgentRuleStat]
    let byCrew: [String: AgentCrewStat]?
    let warnings: [String]?

    enum CodingKeys: String, CodingKey {
        case steps
        case dtMin = "dt_min"
        case nEvents = "n_events"
        case totalPersonMinutes = "total_person_minutes"
        case byRule = "by_rule"
        case byCrew = "by_crew"
        case warnings
    }
}

struct AgentRuleStat: Decodable {
    let events: Int
    let minutes: Double
    let personMinutes: Double
    let peopleMax: Int

    enum CodingKeys: String, CodingKey {
        case events, minutes
        case personMinutes = "person_minutes"
        case peopleMax = "people_max"
    }
}

struct AgentCrewStat: Decodable {
    let events: Int
    let personMinutes: Double

    enum CodingKeys: String, CodingKey {
        case events
        case personMinutes = "person_minutes"
    }
}

struct ContextInfo: Decodable {
    let warnings: [String]
}

struct ModelInfo: Decodable {
    let sourceFile: String
    let elementCount: Int
    let triangleCount: Int
    let failedElementCount: Int

    enum CodingKeys: String, CodingKey {
        case sourceFile = "source_file"
        case elementCount = "element_count"
        case triangleCount = "triangle_count"
        case failedElementCount = "failed_element_count"
    }
}

struct Timeline: Decodable {
    let meta: TimelineMeta
    let crane: Crane
    let zones: [Zone]
    let crews: [Crew]
    let frames: [TimelineFrame]
}

struct TimelineMeta: Decodable {
    let project: String?
    let rulesVersion: String?
    let tStart: String
    let tEnd: String
    let dtMin: Double?

    enum CodingKeys: String, CodingKey {
        case project
        case rulesVersion = "rules_version"
        case tStart = "t_start"
        case tEnd = "t_end"
        case dtMin = "dt_min"
    }
}

struct Crane: Decodable {
    let id: String
    let model: String
    let mast: CraneMast?
    let limits: CraneLimits
    let jib: CraneJib
    let loadChart: [CraneLoadPoint]

    enum CodingKeys: String, CodingKey {
        case id, model, mast, limits, jib
        case loadChart = "load_chart"
    }
}

struct CraneMast: Decodable {
    let x: Double
    let y: Double
    let baseZ: Double
    let topZ: Double

    enum CodingKeys: String, CodingKey {
        case x, y
        case baseZ = "base_z"
        case topZ = "top_z"
    }
}

struct CraneJib: Decodable {
    let lengthM: Double
    let counterJibM: Double?
    let headZM: Double?

    enum CodingKeys: String, CodingKey {
        case lengthM = "length_m"
        case counterJibM = "counter_jib_m"
        case headZM = "head_z"
    }
}

struct CraneLoadPoint: Decodable {
    let radiusM: Double
    let maxLoadKg: Double

    enum CodingKeys: String, CodingKey {
        case radiusM = "radius_m"
        case maxLoadKg = "max_load_kg"
    }
}

struct CraneLimits: Decodable {
    let windLimitOpsMs: Double?
    let windLimitClimbingMs: Double?

    enum CodingKeys: String, CodingKey {
        case windLimitOpsMs = "wind_limit_ops_ms"
        case windLimitClimbingMs = "wind_limit_climbing_ms"
    }
}

struct Zone: Decodable, Identifiable {
    let id: String
    let level: String
    let type: String
    let xMin: Double
    let yMin: Double
    let xMax: Double
    let yMax: Double
    let zFloor: Double
    let zTop: Double

    enum CodingKeys: String, CodingKey {
        case id, level, type
        case xMin = "x_min"
        case yMin = "y_min"
        case xMax = "x_max"
        case yMax = "y_max"
        case zFloor = "z_floor"
        case zTop = "z_top"
    }
}

struct Crew: Decodable, Identifiable {
    let crewId: String
    let name: String
    let size: Int
    let color: String

    var id: String { crewId }

    enum CodingKeys: String, CodingKey {
        case crewId = "crew_id"
        case name, size, color
    }
}

struct TimelineFrame: Decodable {
    let t: String
    let wind: WindState?
    let crane: CraneState?
    let loads: [LoadState]
    let tasksActive: [String]?

    enum CodingKeys: String, CodingKey {
        case t, wind, crane, loads
        case tasksActive = "tasks_active"
    }
}

struct WindState: Decodable {
    let gustAtRefMs: Double
    let gustAtTopMs: Double
    let meanAtRefMs: Double

    enum CodingKeys: String, CodingKey {
        case gustAtRefMs = "gust_at_ref_ms"
        case gustAtTopMs = "gust_at_top_ms"
        case meanAtRefMs = "mean_at_ref_ms"
    }
}

struct CraneState: Decodable {
    let slewDeg: Double
    let radiusM: Double
    let hookZM: Double
    let radiusRawM: Double?
    let jibHeadZM: Double?
    let mastX: Double?
    let mastY: Double?
    let capacityKg: Double?

    enum CodingKeys: String, CodingKey {
        case slewDeg = "slew_deg"
        case radiusM = "radius_m"
        case hookZM = "hook_z_m"
        case radiusRawM = "radius_raw_m"
        case jibHeadZM = "jib_head_z_m"
        case mastX = "mast_x"
        case mastY = "mast_y"
        case capacityKg = "capacity_kg"
    }
}

struct LoadState: Decodable, Identifiable {
    let liftId: String
    let loadName: String?
    let massKg: Double?
    let phase: String
    let x: Double
    let y: Double
    let z: Double
    let swingThetaDeg: Double?

    var id: String { liftId }

    enum CodingKeys: String, CodingKey {
        case liftId = "lift_id"
        case loadName = "load_name"
        case massKg = "mass_kg"
        case phase, x, y, z
        case swingThetaDeg = "swing_theta_deg"
    }
}

struct Finding: Decodable, Identifiable {
    let ruleId: String
    let ruleTitle: String
    let severity: String
    let source: String
    let message: String
    let start: String
    let end: String
    let zoneIds: [String]
    let peopleExposed: Int?

    var id: String { "\(ruleId)|\(start)|\(zoneIds.joined(separator: ","))|\(message)" }

    enum CodingKeys: String, CodingKey {
        case ruleId = "rule_id"
        case ruleTitle = "rule_title"
        case severity, source, message, start, end
        case zoneIds = "zone_ids"
        case peopleExposed = "people_exposed"
    }
}

struct LiftAssessment: Decodable, Identifiable {
    let liftId: String
    let taskId: String
    let loadName: String
    let window: [String]?
    let input: LiftInput
    let summary: LiftSummary
    let swing: SwingInfo?
    let daf: Double?
    let violations: [LiftViolation]?

    var id: String { liftId }

    enum CodingKeys: String, CodingKey {
        case liftId = "lift_id"
        case taskId = "task_id"
        case loadName = "load_name"
        case window, input, summary, swing, daf, violations
    }
}

struct LiftViolation: Decodable, Identifiable {
    let ruleId: String
    let title: String
    let severity: String
    let source: String?
    let message: String

    var id: String { ruleId }

    enum CodingKeys: String, CodingKey {
        case ruleId = "rule_id"
        case title, severity, source, message
    }
}

struct SwingInfo: Decodable {
    let movingAnchor: Bool?
    let collisionCount: Int?
    let structuralCollisionCount: Int?
    let contactMaxNormalForceN: Double?
    let contactMaxImpactSpeedM: Double?
    let contactForceConverged: Bool?
    let naturalPeriodS: Double?
    let steadyOffsetM: Double?

    enum CodingKeys: String, CodingKey {
        case movingAnchor = "moving_anchor"
        case collisionCount = "collision_count"
        case structuralCollisionCount = "structural_collision_count"
        case contactMaxNormalForceN = "contact_max_normal_force_N"
        case contactMaxImpactSpeedM = "contact_max_impact_speed_m_s"
        case contactForceConverged = "contact_force_converged"
        case naturalPeriodS = "natural_period_s"
        case steadyOffsetM = "steady_offset_m"
    }
}

struct LiftInput: Decodable {
    let massKg: Double
    let areaM2: Double
    let windLimitMs: Double
    let radiusM: Double
    let hookHeightM: Double

    enum CodingKeys: String, CodingKey {
        case massKg = "mass_kg"
        case areaM2 = "area_m2"
        case windLimitMs = "wind_limit_ms"
        case radiusM = "radius_m"
        case hookHeightM = "hook_height_m"
    }
}

struct LiftSummary: Decodable {
    let vWorkMaxMs: Double
    let windForceMaxN: Double
    let slingUtilizationMax: Double
    let taglineDeficitMaxN: Double
    let craneUtilizationMax: Double?
    let slingTensionMaxN: Double?
    let craneCapacityKgAtRadius: Double?
    let windLimitExceededHours: Double?
    let safeWindows: [SafeWindow]?

    enum CodingKeys: String, CodingKey {
        case vWorkMaxMs = "v_work_max_ms"
        case windForceMaxN = "wind_force_max_N"
        case slingUtilizationMax = "sling_utilization_max"
        case taglineDeficitMaxN = "tagline_deficit_max_N"
        case craneUtilizationMax = "crane_utilization_max"
        case slingTensionMaxN = "sling_tension_max_N"
        case craneCapacityKgAtRadius = "crane_capacity_kg_at_radius"
        case windLimitExceededHours = "wind_limit_exceeded_hours"
        case safeWindows = "safe_windows"
    }
}

struct SafeWindow: Decodable, Identifiable {
    let start: String
    let end: String
    let hours: Double
    let inTaskWindow: Bool
    let shiftFromTaskStartH: Double?
    let limitRefMs: Double?

    var id: String { "\(start)|\(end)" }

    enum CodingKeys: String, CodingKey {
        case start, end, hours
        case inTaskWindow = "in_task_window"
        case shiftFromTaskStartH = "shift_from_task_start_h"
        case limitRefMs = "limit_ref_ms"
    }
}
