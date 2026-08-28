import Combine
import Foundation

@MainActor
final class AppModel: ObservableObject {
    enum Activity: Equatable {
        case idle
        case pairing
        case listening
        case sending
        case success(String)
        case failure(String)
    }

    @Published private(set) var credentials: RoomAgentCredentials?
    @Published var pendingInvitation: PairingInvitation?
    @Published private(set) var activity: Activity = .idle
    @Published var language: VoiceLanguage {
        didSet { UserDefaults.standard.set(language.rawValue, forKey: Self.languageKey) }
    }
    @Published var manualPairingLink = ""

    let speech = SpeechController()
    private let client = RoomAgentClient()
    private let keychain = KeychainStore()
    private static let languageKey = "room-agent.voice-language"

    init() {
        language = VoiceLanguage(
            rawValue: UserDefaults.standard.string(forKey: Self.languageKey) ?? ""
        ) ?? .english
        do {
            credentials = try keychain.load()
        } catch {
            activity = .failure(error.localizedDescription)
        }
    }

    func receivePairingURL(_ url: URL) {
        do {
            pendingInvitation = try PairingInvitation(url: url)
        } catch {
            activity = .failure(error.localizedDescription)
        }
    }

    func useManualPairingLink() {
        receivePairingText(manualPairingLink)
    }

    func receivePairingText(_ text: String) {
        guard let url = URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            activity = .failure(PairingLinkError.invalidLink.localizedDescription)
            return
        }
        receivePairingURL(url)
    }

    func confirmPairing() async {
        guard let invitation = pendingInvitation else { return }
        pendingInvitation = nil
        activity = .pairing
        do {
            let paired = try await client.redeem(invitation)
            try keychain.save(paired)
            credentials = paired
            manualPairingLink = ""
            let permissionsReady = await speech.requestPermissions()
            activity = permissionsReady
                ? .success("Connected to \(paired.serverURL.host ?? "Room Agent")")
                : .failure("Connected, but Microphone and Speech Recognition access are still required.")
        } catch {
            activity = .failure(error.localizedDescription)
        }
    }

    func startListening() async {
        guard credentials != nil else {
            activity = .failure("Pair this iPhone with a Room Agent computer first.")
            return
        }
        guard !speech.isBusy else { return }
        activity = .listening
        do {
            try await speech.start(language: language) { [weak self] result in
                Task { @MainActor in
                    switch result {
                    case .success(let transcript):
                        await self?.send(transcript)
                    case .failure(let error):
                        self?.activity = .failure(error.localizedDescription)
                    }
                }
            }
        } catch is CancellationError {
            activity = .idle
        } catch {
            activity = .failure(error.localizedDescription)
        }
    }

    func stopListening() {
        speech.finishCapture()
    }

    func cancelListeningForBackground() {
        guard speech.isBusy else { return }
        speech.cancel()
        activity = .idle
    }

    func disconnect() {
        speech.cancel()
        do {
            try keychain.delete()
            credentials = nil
            activity = .idle
        } catch {
            activity = .failure(error.localizedDescription)
        }
    }

    private func send(_ transcript: String) async {
        guard let credentials else { return }
        activity = .sending
        do {
            let response = try await client.send(transcript, using: credentials)
            activity = .success(response.displayMessage)
        } catch RoomAgentClientError.unauthorized {
            try? keychain.delete()
            self.credentials = nil
            activity = .failure(RoomAgentClientError.unauthorized.localizedDescription)
        } catch {
            activity = .failure(error.localizedDescription)
        }
    }
}
