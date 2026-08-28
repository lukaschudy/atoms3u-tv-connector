import XCTest
@testable import RoomAgentApp

final class PairingLinkTests: XCTestCase {
    func testValidPairingLinkParsesServerAndCode() throws {
        let code = "rap1.0123456789abcdef." + String(repeating: "A", count: 43)
        var components = URLComponents()
        components.scheme = "roomagent"
        components.host = "pair"
        components.queryItems = [
            URLQueryItem(name: "server", value: "https://desktop.example.ts.net"),
            URLQueryItem(name: "code", value: code),
        ]

        let invitation = try PairingInvitation(url: XCTUnwrap(components.url))

        XCTAssertEqual(invitation.serverURL.absoluteString, "https://desktop.example.ts.net")
        XCTAssertEqual(invitation.code, code)
    }

    func testPairingRejectsNonHTTPSServer() throws {
        let code = "rap1.0123456789abcdef." + String(repeating: "B", count: 43)
        let encoded = "http://desktop.example.ts.net".addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed)!
        let url = try XCTUnwrap(URL(string: "roomagent://pair?server=\(encoded)&code=\(code)"))

        XCTAssertThrowsError(try PairingInvitation(url: url)) { error in
            XCTAssertEqual(error as? PairingLinkError, .invalidServer)
        }
    }

    func testPairingRejectsUnexpectedParameters() throws {
        let code = "rap1.0123456789abcdef." + String(repeating: "C", count: 43)
        let url = try XCTUnwrap(
            URL(string: "roomagent://pair?server=https%3A%2F%2Fdesktop.example.ts.net&code=\(code)&admin=true")
        )

        XCTAssertThrowsError(try PairingInvitation(url: url))
    }

    func testPairingRejectsPublicHTTPSOriginOutsideTailscale() throws {
        let code = "rap1.0123456789abcdef." + String(repeating: "E", count: 43)
        var components = URLComponents()
        components.scheme = "roomagent"
        components.host = "pair"
        components.queryItems = [
            URLQueryItem(name: "server", value: "https://example.com"),
            URLQueryItem(name: "code", value: code),
        ]

        XCTAssertThrowsError(try PairingInvitation(url: XCTUnwrap(components.url))) { error in
            XCTAssertEqual(error as? PairingLinkError, .invalidServer)
        }
    }

    func testConfirmationDisplaysCompleteHTTPSOriginIncludingPort() throws {
        let code = "rap1.0123456789abcdef." + String(repeating: "D", count: 43)
        var components = URLComponents()
        components.scheme = "roomagent"
        components.host = "pair"
        components.queryItems = [
            URLQueryItem(name: "server", value: "https://desktop.example.ts.net:8443"),
            URLQueryItem(name: "code", value: code),
        ]

        let invitation = try PairingInvitation(url: XCTUnwrap(components.url))

        XCTAssertEqual(invitation.serverName, "https://desktop.example.ts.net:8443")
    }
}
