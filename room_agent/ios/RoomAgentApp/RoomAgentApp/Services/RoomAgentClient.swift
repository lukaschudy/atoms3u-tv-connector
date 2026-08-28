import Foundation

private final class NoRedirectSessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

enum RoomAgentClientError: LocalizedError {
    case invalidResponse
    case rejected(String)
    case unauthorized

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "Room Agent returned an invalid response."
        case .rejected(let message):
            return message
        case .unauthorized:
            return "This iPhone is no longer authorized. Pair it again."
        }
    }
}

actor RoomAgentClient {
    private let session: URLSession
    private let redirectDelegate: NoRedirectSessionDelegate?
    private let maximumResponseBytes = 64 * 1_024

    init(session: URLSession? = nil) {
        if let session {
            self.session = session
            self.redirectDelegate = nil
            return
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 25
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpShouldSetCookies = false
        let delegate = NoRedirectSessionDelegate()
        self.redirectDelegate = delegate
        self.session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
    }

    func redeem(_ invitation: PairingInvitation) async throws -> RoomAgentCredentials {
        let url = invitation.serverURL.appendingPathComponent("v1/pairings/redeem")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "version": 1,
            "pairing_code": invitation.code,
        ])
        let (data, response) = try await session.data(for: request)
        guard data.count <= maximumResponseBytes else {
            throw RoomAgentClientError.invalidResponse
        }
        guard let http = response as? HTTPURLResponse else {
            throw RoomAgentClientError.invalidResponse
        }
        guard http.statusCode == 201 else {
            throw RoomAgentClientError.rejected(
                http.statusCode == 400
                    ? "This pairing invitation has expired or was already used."
                    : "Room Agent could not pair this iPhone."
            )
        }
        let paired = try JSONDecoder().decode(PairingRedemptionResponse.self, from: data)
        guard
            paired.version == 1,
            paired.status == "paired",
            paired.commandsPath == "/v1/commands",
            paired.deviceID.range(of: #"^[0-9a-f]{16}$"#, options: .regularExpression) != nil,
            (1...80).contains(paired.deviceName.count),
            paired.accessToken.range(
                of: #"^ra1\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$"#,
                options: .regularExpression
            ) != nil
        else {
            throw RoomAgentClientError.invalidResponse
        }
        return RoomAgentCredentials(
            serverURL: invitation.serverURL,
            accessToken: paired.accessToken,
            deviceID: paired.deviceID,
            deviceName: paired.deviceName,
            commandsPath: paired.commandsPath
        )
    }

    static func makeCommandRequest(
        _ transcript: String,
        using credentials: RoomAgentCredentials,
        now: Date = Date(),
        requestID: UUID = UUID()
    ) throws -> URLRequest {
        let cleaned = transcript.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard !cleaned.isEmpty, cleaned.count <= 500 else {
            throw RoomAgentClientError.rejected("The voice command was empty or too long.")
        }
        let requestIDText = requestID.uuidString.lowercased()
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]

        var request = URLRequest(url: credentials.commandsURL)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(credentials.accessToken)", forHTTPHeaderField: "Authorization")
        request.setValue(formatter.string(from: now), forHTTPHeaderField: "X-Room-Agent-Sent-At")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "version": 1,
            "request_id": requestIDText,
            "text": cleaned,
        ])
        return request
    }

    func send(_ transcript: String, using credentials: RoomAgentCredentials) async throws -> RoomAgentCommandResponse {
        let requestID = UUID()
        let request = try Self.makeCommandRequest(
            transcript,
            using: credentials,
            requestID: requestID
        )
        let requestIDText = requestID.uuidString.lowercased()
        let (data, response) = try await session.data(for: request)
        guard data.count <= maximumResponseBytes else {
            throw RoomAgentClientError.invalidResponse
        }
        guard let http = response as? HTTPURLResponse else {
            throw RoomAgentClientError.invalidResponse
        }
        if http.statusCode == 401 { throw RoomAgentClientError.unauthorized }
        let decoded = try? JSONDecoder().decode(RoomAgentCommandResponse.self, from: data)
        guard (200...299).contains(http.statusCode), let decoded else {
            throw RoomAgentClientError.rejected(
                decoded?.message ?? "Room Agent could not execute that command."
            )
        }
        guard
            decoded.version == 1,
            decoded.status == "executed",
            decoded.requestID.caseInsensitiveCompare(requestIDText) == .orderedSame
        else {
            throw RoomAgentClientError.invalidResponse
        }
        return decoded
    }
}
