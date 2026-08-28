import AppIntents
import SwiftUI

struct RootView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ZStack {
            LinearGradient(
                colors: [Color(red: 0.03, green: 0.05, blue: 0.10), Color(red: 0.06, green: 0.18, blue: 0.30)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            ScrollView {
                VStack(spacing: 24) {
                    header
                    if model.credentials == nil {
                        PairingView(model: model)
                    } else {
                        VoiceView(model: model)
                        ActionButtonGuide()
                    }
                }
                .padding(22)
            }
        }
        .preferredColorScheme(.dark)
        .sheet(item: $model.pendingInvitation) { invitation in
            PairingConfirmationView(invitation: invitation) {
                Task { await model.confirmPairing() }
            } onCancel: {
                model.pendingInvitation = nil
            }
        }
    }

    private var header: some View {
        VStack(spacing: 10) {
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 62))
                .symbolRenderingMode(.palette)
                .foregroundStyle(.white, .cyan)
            Text("Room Agent")
                .font(.largeTitle.bold())
            Text(model.credentials == nil ? "Pair once. Then control the room by voice." : "Your voice interface for the room")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 18)
    }
}

private struct PairingView: View {
    @ObservedObject var model: AppModel
    @State private var showingScanner = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("Connect this iPhone", systemImage: "qrcode.viewfinder")
                .font(.title2.bold())
            Text("Ask Room Agent on the computer to show a pairing QR code. Scan it here, review the computer name, then tap Connect.")
                .foregroundStyle(.secondary)

            Button {
                showingScanner = true
            } label: {
                Label("Scan pairing QR", systemImage: "qrcode.viewfinder")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)

            Divider()

            Text("Or paste a pairing link")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            TextField("roomagent://pair?…", text: $model.manualPairingLink, axis: .vertical)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(12)
                .background(.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 12))
            Button("Review connection") {
                model.useManualPairingLink()
            }
            .buttonStyle(.borderedProminent)
            .disabled(model.manualPairingLink.isEmpty)

            activityMessage(model.activity)
        }
        .cardStyle()
        .fullScreenCover(isPresented: $showingScanner) {
            QRScannerView { payload in
                showingScanner = false
                model.receivePairingText(payload)
            } onCancel: {
                showingScanner = false
            }
        }
    }
}

private struct VoiceView: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var speech: SpeechController

    init(model: AppModel) {
        self.model = model
        self.speech = model.speech
    }

    var body: some View {
        VStack(spacing: 20) {
            HStack {
                Label("Connected", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.headline)
                Spacer()
                Picker("Language", selection: $model.language) {
                    ForEach(VoiceLanguage.allCases) { language in
                        Text(language.title).tag(language)
                    }
                }
                .labelsHidden()
            }

            Button {
                if speech.isListening {
                    model.stopListening()
                } else {
                    Task { await model.startListening() }
                }
            } label: {
                ZStack {
                    Circle()
                        .fill(speech.isListening ? Color.red : Color.cyan)
                        .frame(width: 128, height: 128)
                        .shadow(color: (speech.isListening ? Color.red : Color.cyan).opacity(0.45), radius: 24)
                    Image(systemName: speech.isListening ? "stop.fill" : "mic.fill")
                        .font(.system(size: 44, weight: .bold))
                        .foregroundStyle(.white)
                }
            }
            .accessibilityLabel(speech.isListening ? "Stop listening" : "Start listening")

            Text(speech.isListening ? "Listening… pause when you are done" : "Tap, speak, and Room Agent will route the command")
                .font(.headline)
                .multilineTextAlignment(.center)

            if !speech.transcript.isEmpty {
                Text("“\(speech.transcript)”")
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }

            activityMessage(model.activity)

            Button("Disconnect this iPhone", role: .destructive) {
                model.disconnect()
            }
            .font(.footnote)
        }
        .cardStyle()
    }
}

private struct ActionButtonGuide: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Finish Action Button setup", systemImage: "button.programmable")
                .font(.title3.bold())
            setupRow("1", "Open Settings → Action Button")
            setupRow("2", "Choose Shortcut")
            setupRow("3", "Select “Talk to Room Agent”")
            Text("Apple requires this final selection. The shortcut itself is already installed with the app.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            ShortcutsLink()
                .shortcutsLinkStyle(.automaticOutline)
        }
        .cardStyle()
    }

    private func setupRow(_ number: String, _ text: String) -> some View {
        HStack(spacing: 12) {
            Text(number)
                .font(.caption.bold())
                .frame(width: 28, height: 28)
                .background(.cyan.opacity(0.25), in: Circle())
            Text(text)
        }
    }
}

private struct PairingConfirmationView: View {
    let invitation: PairingInvitation
    let onConfirm: () -> Void
    let onCancel: () -> Void

    var body: some View {
        NavigationStack {
            VStack(spacing: 22) {
                Image(systemName: "desktopcomputer.and.arrow.down")
                    .font(.system(size: 54))
                    .foregroundStyle(.cyan)
                Text("Connect to this Room Agent?")
                    .font(.title2.bold())
                Text(invitation.serverName)
                    .font(.headline.monospaced())
                    .multilineTextAlignment(.center)
                Text("The invitation works once and expires shortly. The permanent credential will stay in this iPhone’s Keychain.")
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Button("Connect", action: onConfirm)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                Button("Cancel", role: .cancel, action: onCancel)
            }
            .padding(28)
            .navigationTitle("Pair Room Agent")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium])
    }
}

@ViewBuilder
private func activityMessage(_ activity: AppModel.Activity) -> some View {
    switch activity {
    case .idle:
        EmptyView()
    case .pairing:
        Label("Connecting securely…", systemImage: "lock.fill")
            .foregroundStyle(.secondary)
    case .listening:
        Label("Listening", systemImage: "waveform")
            .foregroundStyle(.cyan)
    case .sending:
        Label("Sending command…", systemImage: "arrow.up.circle")
            .foregroundStyle(.secondary)
    case .success(let message):
        Label(message, systemImage: "checkmark.circle.fill")
            .foregroundStyle(.green)
    case .failure(let message):
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .foregroundStyle(.orange)
    }
}

private extension View {
    func cardStyle() -> some View {
        self
            .padding(20)
            .frame(maxWidth: .infinity)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 24))
            .overlay(
                RoundedRectangle(cornerRadius: 24)
                    .stroke(.white.opacity(0.10), lineWidth: 1)
            )
    }
}
