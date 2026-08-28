import XCTest
@testable import RoomAgentApp

final class RoomAgentClientTests: XCTestCase {
    func testCommandRequestCarriesAnISO8601FreshnessTimestamp() throws {
        let credentials = RoomAgentCredentials(
            serverURL: try XCTUnwrap(URL(string: "https://desktop.example.ts.net")),
            accessToken: "ra1.0123456789abcdef." + String(repeating: "A", count: 43),
            deviceID: "0123456789abcdef",
            deviceName: "Test iPhone",
            commandsPath: "/v1/commands"
        )
        let requestID = try XCTUnwrap(
            UUID(uuidString: "8f39dc0f-f43d-4d89-b3bf-6bf557f80e65")
        )

        let request = try RoomAgentClient.makeCommandRequest(
            "Pause the TV",
            using: credentials,
            now: Date(timeIntervalSince1970: 0),
            requestID: requestID
        )

        XCTAssertEqual(
            request.value(forHTTPHeaderField: "X-Room-Agent-Sent-At"),
            "1970-01-01T00:00:00.000Z"
        )
        let body = try XCTUnwrap(request.httpBody)
        let payload = try XCTUnwrap(
            JSONSerialization.jsonObject(with: body) as? [String: Any]
        )
        XCTAssertEqual(payload["version"] as? Int, 1)
        XCTAssertEqual(
            payload["request_id"] as? String,
            "8f39dc0f-f43d-4d89-b3bf-6bf557f80e65"
        )
        XCTAssertEqual(payload["text"] as? String, "Pause the TV")
    }
}
