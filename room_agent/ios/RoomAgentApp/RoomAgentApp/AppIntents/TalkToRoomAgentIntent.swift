import AppIntents
import Combine
import Foundation

@MainActor
final class VoiceLaunchCenter: ObservableObject {
    static let shared = VoiceLaunchCenter()
    @Published private(set) var requestID = UUID()

    private let pendingKey = "room-agent.pending-voice-request-time"
    private let maximumRequestAge: TimeInterval = 15

    private init() {}

    func requestStart() {
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: pendingKey)
        requestID = UUID()
    }

    func consumePendingRequest() -> Bool {
        let requestedAt = UserDefaults.standard.double(forKey: pendingKey)
        UserDefaults.standard.removeObject(forKey: pendingKey)
        guard requestedAt > 0 else { return false }
        let age = Date().timeIntervalSince1970 - requestedAt
        return age >= 0 && age <= maximumRequestAge
    }
}

struct TalkToRoomAgentIntent: AppIntent {
    static let title: LocalizedStringResource = "Talk to Room Agent"
    static let description = IntentDescription("Open Room Agent and listen for one voice command.")
    static let supportedModes: IntentModes = [.foreground(.immediate)]
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication

    func perform() async throws -> some IntentResult {
        await VoiceLaunchCenter.shared.requestStart()
        return .result()
    }
}

struct RoomAgentShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: TalkToRoomAgentIntent(),
            phrases: [
                "Talk to \(.applicationName)",
                "Ask \(.applicationName)",
                "Start \(.applicationName)",
            ],
            shortTitle: "Talk to Room Agent",
            systemImageName: "waveform.circle.fill"
        )
    }

    static var shortcutTileColor: ShortcutTileColor = .blue
}
