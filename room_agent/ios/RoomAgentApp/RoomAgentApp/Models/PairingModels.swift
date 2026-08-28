import Foundation

enum PairingLinkError: LocalizedError, Equatable {
    case invalidLink
    case invalidServer
    case invalidCode

    var errorDescription: String? {
        switch self {
        case .invalidLink:
            return "This is not a Room Agent pairing link."
        case .invalidServer:
            return "The pairing link does not contain a valid private HTTPS server."
        case .invalidCode:
            return "The pairing invitation is invalid or incomplete."
        }
    }
}

struct PairingInvitation: Identifiable, Equatable, Sendable {
    let serverURL: URL
    let code: String

    var id: String { code }
    var serverName: String {
        let value = serverURL.absoluteString
        return value.hasSuffix("/") ? String(value.dropLast()) : value
    }

    init(url: URL) throws {
        guard
            let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            components.scheme?.lowercased() == "roomagent",
            components.host?.lowercased() == "pair"
        else {
            throw PairingLinkError.invalidLink
        }
        let allowedNames = Set(["server", "code"])
        let items = components.queryItems ?? []
        guard Set(items.map(\.name)) == allowedNames, items.count == 2 else {
            throw PairingLinkError.invalidLink
        }
        guard
            let rawServer = items.first(where: { $0.name == "server" })?.value,
            let server = URL(string: rawServer),
            Self.isValidServer(server)
        else {
            throw PairingLinkError.invalidServer
        }
        guard
            let code = items.first(where: { $0.name == "code" })?.value,
            code.range(
                of: #"^rap1\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$"#,
                options: .regularExpression
            ) != nil
        else {
            throw PairingLinkError.invalidCode
        }
        self.serverURL = server
        self.code = code
    }

    private static func isValidServer(_ url: URL) -> Bool {
        guard
            let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            components.scheme?.lowercased() == "https",
            let host = components.host?.lowercased(),
            host.hasSuffix(".ts.net") || host == "localhost" || host == "127.0.0.1",
            components.user == nil,
            components.password == nil,
            components.query == nil,
            components.fragment == nil,
            components.path.isEmpty || components.path == "/"
        else {
            return false
        }
        return true
    }
}

struct RoomAgentCredentials: Codable, Equatable, Sendable {
    let serverURL: URL
    let accessToken: String
    let deviceID: String
    let deviceName: String
    let commandsPath: String

    var commandsURL: URL {
        serverURL.appendingPathComponent(
            commandsPath.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        )
    }
}

struct ShortcutCard: Decodable, Sendable {
    let style: String
    let icon: String
    let title: String
    let detail: String
    let subtitle: String
}

struct PairingRedemptionResponse: Decodable, Sendable {
    let version: Int
    let status: String
    let deviceID: String
    let deviceName: String
    let accessToken: String
    let commandsPath: String

    enum CodingKeys: String, CodingKey {
        case version, status
        case deviceID = "device_id"
        case deviceName = "device_name"
        case accessToken = "access_token"
        case commandsPath = "commands_path"
    }
}

struct RoomAgentCommandResponse: Decodable, Sendable {
    let version: Int
    let requestID: String
    let status: String
    let popup: String?
    let action: String?
    let message: String?
    let shortcutCard: ShortcutCard?

    var displayMessage: String {
        if let card = shortcutCard {
            return ["\(card.icon)  \(card.title)", card.detail, card.subtitle]
                .filter { !$0.isEmpty }
                .joined(separator: "\n")
        }
        return popup ?? message ?? "Command completed"
    }

    enum CodingKeys: String, CodingKey {
        case version, status, popup, action, message
        case requestID = "request_id"
        case shortcutCard = "shortcut_card"
    }
}
