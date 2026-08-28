import AppIntents
import Combine
import SwiftUI

@main
struct RoomAgentApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        RoomAgentShortcuts.updateAppShortcutParameters()
    }

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .onReceive(VoiceLaunchCenter.shared.$requestID.dropFirst()) { _ in
                    beginVoiceRequestIfNeeded()
                }
                .task {
                    beginVoiceRequestIfNeeded()
                }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                beginVoiceRequestIfNeeded()
            } else {
                model.cancelListeningForBackground()
            }
        }
    }

    private func beginVoiceRequestIfNeeded() {
        guard VoiceLaunchCenter.shared.consumePendingRequest() else { return }
        Task { await model.startListening() }
    }
}
