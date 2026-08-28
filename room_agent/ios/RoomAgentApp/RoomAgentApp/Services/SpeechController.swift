import AVFAudio
import Combine
import Foundation
import Speech

enum VoiceLanguage: String, CaseIterable, Identifiable {
    case english
    case czech

    var id: String { rawValue }
    var title: String { self == .english ? "English" : "Čeština" }
    var locale: Locale { Locale(identifier: self == .english ? "en-US" : "cs-CZ") }
}

enum SpeechControllerError: LocalizedError {
    case permissionDenied
    case languageUnavailable
    case inputUnavailable
    case noSpeech

    var errorDescription: String? {
        switch self {
        case .permissionDenied:
            return "Allow Microphone and Speech Recognition access in Settings."
        case .languageUnavailable:
            return "Speech recognition is unavailable for this language."
        case .inputUnavailable:
            return "No usable microphone input is available."
        case .noSpeech:
            return "I didn’t hear a command. Please try again."
        }
    }
}

@MainActor
final class SpeechController: ObservableObject {
    @Published private(set) var transcript = ""
    @Published private(set) var isListening = false
    @Published private(set) var isStarting = false

    var isBusy: Bool { isStarting || isListening || isCompleting }

    private let audioEngine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var silenceTask: Task<Void, Never>?
    private var maximumTask: Task<Void, Never>?
    private var finalizationTask: Task<Void, Never>?
    private var completion: ((Result<String, Error>) -> Void)?
    private var isCompleting = false
    private var tapInstalled = false

    func requestPermissions() async -> Bool {
        let speech = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
        let microphone = await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
        return speech && microphone
    }

    func start(
        language: VoiceLanguage,
        completion: @escaping (Result<String, Error>) -> Void
    ) async throws {
        guard !isBusy else { return }
        isStarting = true
        defer { isStarting = false }
        guard await requestPermissions() else { throw SpeechControllerError.permissionDenied }
        guard isStarting else { throw CancellationError() }
        guard let recognizer = SFSpeechRecognizer(locale: language.locale), recognizer.isAvailable else {
            throw SpeechControllerError.languageUnavailable
        }

        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.record, mode: .measurement, options: [])
        try audioSession.setActive(true)

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.taskHint = .confirmation
        if recognizer.supportsOnDeviceRecognition {
            request.requiresOnDeviceRecognition = true
        }
        self.request = request
        self.completion = completion
        transcript = ""
        isCompleting = false

        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            self.request = nil
            self.completion = nil
            try? audioSession.setActive(false, options: .notifyOthersOnDeactivation)
            throw SpeechControllerError.inputUnavailable
        }
        input.installTap(onBus: 0, bufferSize: 1_024, format: format) { buffer, _ in
            request.append(buffer)
        }
        tapInstalled = true
        audioEngine.prepare()
        do {
            try audioEngine.start()
        } catch {
            removeInputTapIfNeeded()
            self.request = nil
            self.completion = nil
            try? audioSession.setActive(false, options: .notifyOthersOnDeactivation)
            throw error
        }
        isListening = true

        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in
                guard let self else { return }
                if let result {
                    self.receive(result.bestTranscription.formattedString, isFinal: result.isFinal)
                } else if let error, !self.isCompleting {
                    self.complete(.failure(error))
                }
            }
        }
        maximumTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 12_000_000_000)
            guard !Task.isCancelled else { return }
            await self?.finishCapture()
        }
    }

    func finishCapture() {
        guard isListening, !isCompleting else { return }
        isCompleting = true
        isListening = false
        silenceTask?.cancel()
        maximumTask?.cancel()
        if audioEngine.isRunning { audioEngine.stop() }
        removeInputTapIfNeeded()
        request?.endAudio()
        finalizationTask?.cancel()
        finalizationTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 500_000_000)
            guard !Task.isCancelled else { return }
            await self?.completeCurrentTranscript()
        }
    }

    func cancel() {
        silenceTask?.cancel()
        maximumTask?.cancel()
        finalizationTask?.cancel()
        finalizationTask = nil
        recognitionTask?.cancel()
        recognitionTask = nil
        request = nil
        if audioEngine.isRunning { audioEngine.stop() }
        removeInputTapIfNeeded()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        completion = nil
        isStarting = false
        isListening = false
        isCompleting = false
    }

    private func receive(_ text: String, isFinal: Bool) {
        transcript = text
        if isFinal {
            completeCurrentTranscript()
            return
        }
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        silenceTask?.cancel()
        silenceTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            guard !Task.isCancelled else { return }
            await self?.finishCapture()
        }
    }

    private func completeCurrentTranscript() {
        let cleaned = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        complete(cleaned.isEmpty ? .failure(SpeechControllerError.noSpeech) : .success(cleaned))
    }

    private func complete(_ result: Result<String, Error>) {
        guard let completion else { return }
        self.completion = nil
        silenceTask?.cancel()
        maximumTask?.cancel()
        finalizationTask?.cancel()
        finalizationTask = nil
        recognitionTask?.cancel()
        recognitionTask = nil
        request = nil
        if audioEngine.isRunning { audioEngine.stop() }
        removeInputTapIfNeeded()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        isListening = false
        isCompleting = false
        completion(result)
    }

    private func removeInputTapIfNeeded() {
        guard tapInstalled else { return }
        audioEngine.inputNode.removeTap(onBus: 0)
        tapInstalled = false
    }
}
