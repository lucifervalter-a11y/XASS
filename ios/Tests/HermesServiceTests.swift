import XCTest
@testable import XASS

final class HermesServiceTests: XCTestCase {

    func testOfflineStatusIsUnavailableAndEmpty() {
        let status = HermesStatus.offline

        XCTAssertFalse(status.isAvailable)
        XCTAssertFalse(status.isInteractiveAvailable)
        XCTAssertTrue(status.modelName.isEmpty)
        XCTAssertEqual(status.activeSessionsCount, 0)
        XCTAssertEqual(status.latencyMs, 0)
        XCTAssertTrue(status.version.isEmpty)
        XCTAssertTrue(status.features.isEmpty)
        XCTAssertTrue(status.statusDescription.contains("XASS AI"))
    }

    func testDefaultSessionsAreNotFabricated() {
        XCTAssertTrue(HermesSession.defaultSessions.isEmpty)
    }

    func testHermesMessageCreationKeepsVerifiedResponseState() {
        let message = HermesMessage(
            role: .assistant,
            content: "Подтверждённый ответ",
            isReasoning: false,
            reasoningSteps: [],
            reasoningContent: nil,
            tokensUsed: 42,
            isOfflineFallback: false
        )

        XCTAssertEqual(message.role, .assistant)
        XCTAssertEqual(message.content, "Подтверждённый ответ")
        XCTAssertFalse(message.isReasoning)
        XCTAssertTrue(message.reasoningSteps.isEmpty)
        XCTAssertNil(message.reasoningContent)
        XCTAssertEqual(message.tokensUsed, 42)
        XCTAssertFalse(message.isOfflineFallback)
    }

    @MainActor
    func testHermesServiceStartsUnavailableWithoutSeedData() {
        let service = HermesService()

        XCTAssertFalse(service.status.isAvailable)
        XCTAssertNil(service.activeSessionID)
        XCTAssertTrue(service.sessions.isEmpty)
        XCTAssertTrue(service.messages.isEmpty)
        XCTAssertTrue(service.allSessionMessages.isEmpty)
        XCTAssertFalse(service.isThinking)
        XCTAssertNil(service.errorMessage)
    }

    @MainActor
    func testFetchWithoutAPIStaysUnavailableAndDoesNotSeedSessions() async {
        let service = HermesService()

        let status = await service.fetchStatus()
        XCTAssertFalse(status.isAvailable)
        XCTAssertNotNil(service.errorMessage)

        let sessions = await service.fetchSessions()
        XCTAssertTrue(sessions.isEmpty)
        XCTAssertTrue(service.sessions.isEmpty)
        XCTAssertNil(service.activeSessionID)
        XCTAssertTrue(service.messages.isEmpty)
        XCTAssertFalse(service.status.isAvailable)
    }

    @MainActor
    func testOfflineStatusPayloadIsNotPromotedToAvailable() async {
        let api = OwnerServiceStub(responses: [
            "/api/mini/hermes/status": [
                "ok": true,
                "status": [
                    "online": false,
                    "enabled": true,
                    "version": "offline",
                    "active_model": "none",
                    "session_count": 3,
                    "mock": true
                ]
            ]
        ])
        let service = HermesService(api: api)

        let status = await service.fetchStatus()

        XCTAssertFalse(status.isAvailable)
        XCTAssertTrue(status.modelName.isEmpty)
        XCTAssertEqual(status.activeSessionsCount, 0)
        XCTAssertTrue(status.version.isEmpty)
        XCTAssertNotNil(service.errorMessage)
    }

    @MainActor
    func testVerifiedHealthWithoutInteractiveFlagKeepsChatDisabled() async {
        let api = OwnerServiceStub(responses: [
            "/api/mini/hermes/status": [
                "ok": true,
                "status": [
                    "online": true,
                    "enabled": true,
                    "version": "0.22.2",
                    "active_model": "",
                    "session_count": 0,
                    "mock": false
                ]
            ]
        ])
        let service = HermesService(api: api)

        let status = await service.fetchStatus()

        XCTAssertTrue(status.isAvailable)
        XCTAssertFalse(status.isInteractiveAvailable)
        XCTAssertNotNil(service.errorMessage)

        do {
            _ = try await service.createRemoteSession()
            XCTFail("Health-only status must not enable interactive controls")
        } catch {
            XCTAssertEqual((error as NSError).code, 503)
        }
        XCTAssertTrue(service.sessions.isEmpty)
        XCTAssertEqual(api.requestedPaths, ["/api/mini/hermes/status"])
    }

    @MainActor
    func testRemoteSessionCreationUsesConfirmedServerSession() async throws {
        let api = OwnerServiceStub(responses: [
            "/api/mini/hermes/status": [
                "ok": true,
                "status": [
                    "online": true,
                    "enabled": true,
                    "version": "future-compatible",
                    "active_model": "verified-model",
                    "session_count": 0,
                    "mock": false,
                    "interactive_available": true
                ]
            ],
            "/api/mini/hermes/sessions": [
                "ok": true,
                "session": [
                    "id": "server-session",
                    "title": "Новый диалог",
                    "message_count": 0,
                    "mock": false
                ]
            ]
        ])
        let service = HermesService(api: api)

        let status = await service.fetchStatus()
        XCTAssertTrue(status.isInteractiveAvailable)
        let session = try await service.createRemoteSession()

        XCTAssertEqual(session.id, "server-session")
        XCTAssertEqual(service.sessions, [session])
        XCTAssertEqual(service.activeSessionID, session.id)
        XCTAssertTrue(service.messages.isEmpty)
        XCTAssertNil(service.errorMessage)
    }

    @MainActor
    func testEmptyPromptThrowsWithoutChangingState() async {
        let service = HermesService()

        do {
            _ = try await service.sendMessage("   ")
            XCTFail("Empty prompt should throw an error")
        } catch {
            XCTAssertEqual((error as NSError).code, 400)
        }

        XCTAssertTrue(service.messages.isEmpty)
        XCTAssertTrue(service.sessions.isEmpty)
        XCTAssertFalse(service.isThinking)
    }

    @MainActor
    func testSendWithoutAPIThrowsAndDoesNotFabricateAnswer() async {
        let service = HermesService()
        service.status = HermesStatus(isAvailable: true, interactiveAvailable: true)

        do {
            _ = try await service.sendMessage("Проверить статус")
            XCTFail("Unavailable service should throw an error")
        } catch {
            XCTAssertEqual((error as NSError).code, 503)
        }

        XCTAssertFalse(service.status.isAvailable)
        XCTAssertNotNil(service.errorMessage)
        XCTAssertTrue(service.sessions.isEmpty)
        XCTAssertNil(service.activeSessionID)
        XCTAssertTrue(service.messages.isEmpty)
        XCTAssertTrue(service.allSessionMessages.isEmpty)
        XCTAssertFalse(service.isThinking)
        XCTAssertTrue(service.currentReasoningStep.isEmpty)
    }

    @MainActor
    func testMockChatResponseIsRejectedWithoutAppendingMessages() async {
        let api = OwnerServiceStub(responses: [
            "/api/mini/hermes/chat": [
                "ok": true,
                "response": "Сгенерированный fallback",
                "tokens_used": 99,
                "mock": true
            ]
        ])
        let service = HermesService(api: api)
        service.status = HermesStatus(isAvailable: true, interactiveAvailable: true)
        service.activeSessionID = "server-session"
        service.sessions = [HermesSession(id: "server-session", title: "Диалог")]

        do {
            _ = try await service.sendMessage("Проверить статус")
            XCTFail("Mock response should be rejected")
        } catch {
            XCTAssertEqual((error as NSError).code, 503)
        }

        XCTAssertFalse(service.status.isAvailable)
        XCTAssertNotNil(service.errorMessage)
        XCTAssertTrue(service.messages.isEmpty)
        XCTAssertTrue(service.allSessionMessages.isEmpty)
        XCTAssertFalse(service.isThinking)
    }
}

private final class OwnerServiceStub: OwnerService {
    let origin: ServerOrigin
    private let responses: [String: [String: Any]]
    private(set) var requestedPaths: [String] = []

    init(responses: [String: [String: Any]]) {
        origin = try! ServerOrigin("https://xass.example")
        self.responses = responses
    }

    @MainActor
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        requestedPaths.append(path)
        guard let response = responses[path] else {
            throw OwnerAPIError.invalidResponse
        }
        return response
    }
}
