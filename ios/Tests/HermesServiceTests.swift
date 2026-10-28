import XCTest
@testable import XASS

final class HermesServiceTests: XCTestCase {

    func testHermesStatusMockOffline() {
        let status = HermesStatus.mockOffline
        XCTAssertTrue(status.isAvailable)
        XCTAssertTrue(status.modelName.contains("Hermes"))
        XCTAssertGreaterThan(status.activeSessionsCount, 0)
        XCTAssertGreaterThan(status.latencyMs, 0)
        XCTAssertFalse(status.version.isEmpty)
        XCTAssertFalse(status.features.isEmpty)
    }

    func testHermesSessionDefaultList() {
        let sessions = HermesSession.defaultSessions
        XCTAssertGreaterThanOrEqual(sessions.count, 3)
        XCTAssertTrue(sessions.contains(where: { $0.id == "session-general" }))
        XCTAssertTrue(sessions.contains(where: { $0.id == "session-music" }))
        XCTAssertTrue(sessions.contains(where: { $0.id == "session-automation" }))
    }

    func testHermesMessageCreation() {
        let message = HermesMessage(
            role: .assistant,
            content: "Тестовый ответ",
            isReasoning: true,
            reasoningSteps: ["Шаг 1", "Шаг 2"],
            tokensUsed: 42,
            isOfflineFallback: true
        )

        XCTAssertEqual(message.role, .assistant)
        XCTAssertEqual(message.content, "Тестовый ответ")
        XCTAssertTrue(message.isReasoning)
        XCTAssertEqual(message.reasoningSteps.count, 2)
        XCTAssertEqual(message.tokensUsed, 42)
        XCTAssertTrue(message.isOfflineFallback)
    }

    @MainActor
    func testHermesServiceInitialization() {
        let service = HermesService()
        XCTAssertNotNil(service.activeSessionID)
        XCTAssertFalse(service.sessions.isEmpty)
        XCTAssertFalse(service.messages.isEmpty)
        XCTAssertEqual(service.messages.first?.role, .assistant)
        XCTAssertFalse(service.isThinking)
    }

    @MainActor
    func testFetchStatusAndSessionsFallback() async {
        let service = HermesService()
        let status = await service.fetchStatus()
        XCTAssertTrue(status.isAvailable)

        let sessions = await service.fetchSessions()
        XCTAssertFalse(sessions.isEmpty)
    }

    @MainActor
    func testSessionManagement() {
        let service = HermesService()
        let initialCount = service.sessions.count

        let newSession = service.createSession(title: "Новый тест")
        XCTAssertEqual(service.sessions.count, initialCount + 1)
        XCTAssertEqual(service.activeSessionID, newSession.id)
        XCTAssertTrue(service.messages.isEmpty)

        service.selectSession("session-general")
        XCTAssertEqual(service.activeSessionID, "session-general")
        XCTAssertFalse(service.messages.isEmpty)

        service.deleteSession(newSession.id)
        XCTAssertEqual(service.sessions.count, initialCount)
    }

    @MainActor
    func testSendMessageValidation() async {
        let service = HermesService()
        do {
            try await service.sendMessage("   ")
            XCTFail("Empty prompt should throw an error")
        } catch {
            XCTAssertNotNil(error)
        }
    }

    @MainActor
    func testSendMessageOfflineResponseGeneration() async throws {
        let service = HermesService()
        let prompt = "Какой текущий статус сервера?"

        let initialCount = service.messages.count
        let response = try await service.sendMessage(prompt)

        XCTAssertEqual(response.role, .assistant)
        XCTAssertFalse(response.content.isEmpty)
        XCTAssertTrue(response.content.contains("сервер") || response.content.contains("XASS") || response.content.contains("CPU"))
        XCTAssertTrue(response.isOfflineFallback)
        XCTAssertTrue(response.isReasoning)
        XCTAssertFalse(response.reasoningSteps.isEmpty)
        XCTAssertFalse(service.isThinking)
        XCTAssertEqual(service.messages.count, initialCount + 2) // User + Assistant
    }

    @MainActor
    func testSendMessageCategoryResponses() async throws {
        let service = HermesService()

        // Music prompt
        let musicReply = try await service.sendMessage("Расскажи про музыку и треки")
        XCTAssertTrue(musicReply.content.contains("Музыка") || musicReply.content.contains("трек") || musicReply.content.contains("кэш"))

        // PC Agent prompt
        let pcReply = try await service.sendMessage("Каково состояние ПК агента?")
        XCTAssertTrue(pcReply.content.contains("ПК") || pcReply.content.contains("агент") || pcReply.content.contains("Windows"))

        // Automation prompt
        let autoReply = try await service.sendMessage("Какие сценарии автоматизации настроены?")
        XCTAssertTrue(autoReply.content.contains("сценари") || autoReply.content.contains("правил") || autoReply.content.contains("Автоматизация"))
    }
}
