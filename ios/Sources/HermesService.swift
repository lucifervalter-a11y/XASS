import Foundation
import Combine
import SwiftUI
#if canImport(ActivityKit)
import ActivityKit
#endif

// MARK: - Models

public enum HermesRole: String, Codable, Sendable {
    case user
    case assistant
    case system
    case tool
}

public struct HermesStatus: Codable, Equatable, Sendable {
    public var isAvailable: Bool
    /// `nil` means the backend did not advertise an interactive phone transport.
    public var interactiveAvailable: Bool?
    public var modelName: String
    public var activeSessionsCount: Int
    public var latencyMs: Double
    public var version: String
    public var features: [String]
    public var statusDescription: String

    public init(
        isAvailable: Bool = false,
        interactiveAvailable: Bool? = nil,
        modelName: String = "",
        activeSessionsCount: Int = 0,
        latencyMs: Double = 0,
        version: String = "",
        features: [String] = [],
        statusDescription: String = "XASS AI недоступен"
    ) {
        self.isAvailable = isAvailable
        self.interactiveAvailable = interactiveAvailable
        self.modelName = modelName
        self.activeSessionsCount = activeSessionsCount
        self.latencyMs = latencyMs
        self.version = version
        self.features = features
        self.statusDescription = statusDescription
    }

    /// Fail closed: only an explicit `true` from the backend enables chat controls.
    public var isInteractiveAvailable: Bool {
        interactiveAvailable == true
    }

    public static let offline = HermesStatus()
}

public struct HermesSession: Identifiable, Codable, Equatable, Sendable {
    public var id: String
    public var title: String
    public var createdAt: Date
    public var updatedAt: Date
    public var messageCount: Int
    public var lastMessagePreview: String

    public init(
        id: String = UUID().uuidString,
        title: String,
        createdAt: Date = Date(),
        updatedAt: Date = Date(),
        messageCount: Int = 0,
        lastMessagePreview: String = ""
    ) {
        self.id = id
        self.title = title
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.messageCount = messageCount
        self.lastMessagePreview = lastMessagePreview
    }

    public static let defaultSessions: [HermesSession] = []
}

public struct HermesMessage: Identifiable, Codable, Equatable, Sendable {
    public var id: String
    public var role: HermesRole
    public var content: String
    public var timestamp: Date
    public var isReasoning: Bool
    public var reasoningSteps: [String]
    public var reasoningContent: String?
    public var tokensUsed: Int?
    public var isOfflineFallback: Bool

    public init(
        id: String = UUID().uuidString,
        role: HermesRole = .assistant,
        content: String,
        timestamp: Date = Date(),
        isReasoning: Bool = false,
        reasoningSteps: [String] = [],
        reasoningContent: String? = nil,
        tokensUsed: Int? = nil,
        isOfflineFallback: Bool = false
    ) {
        self.id = id
        self.role = role
        self.content = content
        self.timestamp = timestamp
        self.isReasoning = isReasoning
        self.reasoningSteps = reasoningSteps
        self.reasoningContent = reasoningContent
        self.tokensUsed = tokensUsed
        self.isOfflineFallback = isOfflineFallback
    }
}

// MARK: - Live Activity & Dynamic Island Attributes

#if canImport(ActivityKit)
public struct HermesActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable, Sendable {
        public var queryPreview: String
        public var statusPhase: String
        public var progress: Double
        public var isThinking: Bool
        public var responseTokens: Int
        public var stepDescription: String

        public init(
            queryPreview: String,
            statusPhase: String,
            progress: Double = 0.0,
            isThinking: Bool = true,
            responseTokens: Int = 0,
            stepDescription: String = ""
        ) {
            self.queryPreview = queryPreview
            self.statusPhase = statusPhase
            self.progress = progress
            self.isThinking = isThinking
            self.responseTokens = responseTokens
            self.stepDescription = stepDescription
        }
    }

    public var sessionID: String
    public var sessionTitle: String

    public init(sessionID: String, sessionTitle: String) {
        self.sessionID = sessionID
        self.sessionTitle = sessionTitle
    }
}
#endif

// MARK: - Live Activity Manager

@MainActor
public final class HermesLiveActivityManager {
    public static let shared = HermesLiveActivityManager()

    #if canImport(ActivityKit)
    private var currentActivity: Activity<HermesActivityAttributes>?
    #endif

    private init() {}

    public func startActivity(sessionID: String, sessionTitle: String, prompt: String) {
        #if canImport(ActivityKit)
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let attributes = HermesActivityAttributes(sessionID: sessionID, sessionTitle: sessionTitle)
        let initialContentState = HermesActivityAttributes.ContentState(
            queryPreview: String(prompt.prefix(40)),
            statusPhase: "Отправка запроса...",
            progress: 0.1,
            isThinking: true,
            responseTokens: 0,
            stepDescription: "Ожидание XASS AI"
        )
        do {
            if let existing = currentActivity {
                Task {
                    await existing.end(nil, dismissalPolicy: .immediate)
                }
            }
            currentActivity = try Activity<HermesActivityAttributes>.request(
                attributes: attributes,
                content: .init(state: initialContentState, staleDate: nil)
            )
        } catch {
            // Live activities may be disabled or running in preview
        }
        #endif
    }

    public func updateProgress(stepDescription: String, progress: Double, phase: String) {
        #if canImport(ActivityKit)
        guard let activity = currentActivity else { return }
        var state = activity.content.state
        state.stepDescription = stepDescription
        state.progress = min(max(progress, 0.0), 1.0)
        state.statusPhase = phase
        Task {
            await activity.update(.init(state: state, staleDate: nil))
        }
        #endif
    }

    public func endActivity(finalResponse: String, tokens: Int = 0) {
        #if canImport(ActivityKit)
        guard let activity = currentActivity else { return }
        var state = activity.content.state
        state.isThinking = false
        state.progress = 1.0
        state.statusPhase = "Ответ сформирован"
        state.stepDescription = "Завершено"
        state.responseTokens = tokens
        Task {
            await activity.end(.init(state: state, staleDate: nil), dismissalPolicy: .after(Date().addingTimeInterval(4)))
            self.currentActivity = nil
        }
        #endif
    }

    public func failActivity() {
        #if canImport(ActivityKit)
        guard let activity = currentActivity else { return }
        var state = activity.content.state
        state.isThinking = false
        state.progress = 0
        state.statusPhase = "XASS AI недоступен"
        state.stepDescription = "Запрос не выполнен"
        state.responseTokens = 0
        Task {
            await activity.end(.init(state: state, staleDate: nil), dismissalPolicy: .after(Date().addingTimeInterval(4)))
            self.currentActivity = nil
        }
        #endif
    }
}

// MARK: - Hermes Client Service

@MainActor
public final class HermesService: ObservableObject {
    private static let interactiveUnavailableMessage = "Среда XASS AI доступна, но интерактивный доступ с iPhone не поддерживается."

    @Published public var status: HermesStatus = .offline
    @Published public var sessions: [HermesSession] = HermesSession.defaultSessions
    @Published public var activeSessionID: String? = nil
    @Published public var messages: [HermesMessage] = []
    @Published public var allSessionMessages: [String: [HermesMessage]] = [:]
    @Published public var isThinking: Bool = false
    @Published public var currentReasoningStep: String = ""
    @Published public var errorMessage: String? = nil

    public weak var api: OwnerService?

    public init(api: OwnerService? = nil) {
        self.api = api
    }

    @discardableResult
    private func markUnavailable(_ message: String = "XASS AI недоступен. Проверьте подключение и состояние сервиса.") -> HermesStatus {
        let offline = HermesStatus(statusDescription: message)
        status = offline
        errorMessage = message
        return offline
    }

    private func clearRemoteSessions() {
        sessions = []
        activeSessionID = nil
        messages = []
        allSessionMessages = [:]
    }

    private var interactionBlockMessage: String {
        status.isAvailable
            ? Self.interactiveUnavailableMessage
            : "XASS AI недоступен. Проверьте подключение и состояние сервиса."
    }

    // MARK: - Client API Methods

    /// Fetches the real XASS AI status. Missing or malformed data is unavailable, never success.
    @discardableResult
    public func fetchStatus() async -> HermesStatus {
        guard let api = api else {
            return markUnavailable()
        }

        do {
            let response = try await api.request("/api/mini/hermes/status", method: "GET", body: nil)
            guard response["ok"] as? Bool == true,
                  let statusDict = response["status"] as? [String: Any],
                  let online = statusDict["online"] as? Bool else {
                return markUnavailable("XASS AI вернул неподдерживаемый ответ.")
            }
            let enabled = statusDict["enabled"] as? Bool ?? true
            let isMock = statusDict["mock"] as? Bool ?? false
            guard enabled else {
                return markUnavailable("XASS AI отключён в настройках сервера.")
            }
            guard online && !isMock else {
                return markUnavailable("XASS AI сейчас не в сети.")
            }

            let model = ((statusDict["active_model"] as? String) ?? (statusDict["model_name"] as? String) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let sessionsCount = max(0, (statusDict["session_count"] as? Int) ?? (statusDict["active_sessions"] as? Int) ?? 0)
            let latency = max(0, (statusDict["latency_ms"] as? NSNumber)?.doubleValue ?? 0)
            let version = (statusDict["version"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let features = statusDict["features"] as? [String] ?? []
            let interactiveAvailable = statusDict["interactive_available"] as? Bool
            let description = interactiveAvailable == true
                ? "XASS AI доступен"
                : Self.interactiveUnavailableMessage

            let result = HermesStatus(
                isAvailable: true,
                interactiveAvailable: interactiveAvailable,
                modelName: model,
                activeSessionsCount: sessionsCount,
                latencyMs: latency,
                version: version,
                features: features,
                statusDescription: description
            )
            status = result
            errorMessage = result.isInteractiveAvailable ? nil : description
            return result
        } catch {
            return markUnavailable()
        }
    }

    /// Fetches real sessions. An unavailable endpoint never creates local seed data.
    @discardableResult
    public func fetchSessions() async -> [HermesSession] {
        guard status.isInteractiveAvailable else {
            clearRemoteSessions()
            errorMessage = interactionBlockMessage
            return []
        }
        guard let api = api else {
            clearRemoteSessions()
            markUnavailable()
            return []
        }

        do {
            let response = try await api.request("/api/mini/hermes/sessions", method: "GET", body: nil)
            guard response["ok"] as? Bool == true,
                  let list = response["sessions"] as? [[String: Any]] else {
                clearRemoteSessions()
                markUnavailable("XASS AI вернул неподдерживаемый список диалогов.")
                return []
            }
            guard !list.contains(where: { ($0["mock"] as? Bool) == true }) else {
                clearRemoteSessions()
                markUnavailable("XASS AI сейчас не в сети.")
                return []
            }
            var loaded: [HermesSession] = []
            for item in list {
                if let id = item["id"] as? String, !id.isEmpty,
                   let title = item["title"] as? String, !title.isEmpty {
                    let count = max(0, item["message_count"] as? Int ?? 0)
                    let preview = item["last_preview"] as? String ?? ""
                    loaded.append(HermesSession(id: id, title: title, messageCount: count, lastMessagePreview: preview))
                }
            }
            sessions = loaded
            if let currentSessionID = activeSessionID,
               loaded.contains(where: { $0.id == currentSessionID }) {
                messages = allSessionMessages[currentSessionID] ?? []
            } else {
                activeSessionID = loaded.first?.id
                messages = activeSessionID.flatMap { allSessionMessages[$0] } ?? []
            }
            errorMessage = nil
            return loaded
        } catch {
            clearRemoteSessions()
            markUnavailable()
            return []
        }
    }

    /// Selects active session and loads its messages.
    public func selectSession(_ sessionID: String) {
        guard activeSessionID != sessionID else { return }
        activeSessionID = sessionID
        messages = allSessionMessages[sessionID] ?? []
    }

    /// Creates a session through the authenticated XASS endpoint and activates it.
    @discardableResult
    public func createRemoteSession(title: String? = nil) async throws -> HermesSession {
        guard status.isInteractiveAvailable else {
            let message = interactionBlockMessage
            errorMessage = message
            throw NSError(domain: "XASSAI", code: 503, userInfo: [NSLocalizedDescriptionKey: message])
        }
        guard let api else {
            markUnavailable()
            throw NSError(domain: "XASSAI", code: 503, userInfo: [NSLocalizedDescriptionKey: errorMessage ?? "XASS AI недоступен."])
        }

        do {
            let normalizedTitle = title?.trimmingCharacters(in: .whitespacesAndNewlines)
            var body: [String: Any] = [:]
            if let normalizedTitle, !normalizedTitle.isEmpty {
                body["title"] = normalizedTitle
            }
            let response = try await api.request("/api/mini/hermes/sessions", method: "POST", body: body)
            guard response["ok"] as? Bool == true,
                  let item = response["session"] as? [String: Any],
                  (item["mock"] as? Bool) != true,
                  let id = item["id"] as? String, !id.isEmpty else {
                throw NSError(domain: "XASSAI", code: 502, userInfo: [NSLocalizedDescriptionKey: "XASS AI не подтвердил создание диалога."])
            }

            let serverTitle = (item["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
            let resolvedTitle: String
            if let serverTitle, !serverTitle.isEmpty {
                resolvedTitle = serverTitle
            } else if let normalizedTitle, !normalizedTitle.isEmpty {
                resolvedTitle = normalizedTitle
            } else {
                resolvedTitle = "Новый диалог"
            }
            let session = HermesSession(
                id: id,
                title: resolvedTitle,
                messageCount: max(0, item["message_count"] as? Int ?? 0),
                lastMessagePreview: item["last_preview"] as? String ?? ""
            )
            sessions.removeAll { $0.id == session.id }
            sessions.insert(session, at: 0)
            activeSessionID = session.id
            allSessionMessages[session.id] = []
            messages = []
            errorMessage = nil
            return session
        } catch {
            markUnavailable("Не удалось создать диалог XASS AI.")
            throw NSError(domain: "XASSAI", code: 503, userInfo: [NSLocalizedDescriptionKey: errorMessage ?? "XASS AI недоступен."])
        }
    }

    /// Deletes a session.
    public func deleteSession(_ sessionID: String) {
        sessions.removeAll { $0.id == sessionID }
        allSessionMessages.removeValue(forKey: sessionID)
        if activeSessionID == sessionID {
            activeSessionID = sessions.first?.id
            messages = activeSessionID.flatMap { allSessionMessages[$0] } ?? []
        }
    }

    /// Clears message history for the active session.
    public func clearActiveHistory() {
        guard let sessionID = activeSessionID else { return }
        allSessionMessages[sessionID] = []
        messages = []
    }

    /// Sends a prompt to the real XASS AI endpoint. Errors stay errors; no answer is fabricated.
    @discardableResult
    public func sendMessage(_ prompt: String, sessionID: String? = nil) async throws -> HermesMessage {
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw NSError(domain: "XASSAI", code: 400, userInfo: [NSLocalizedDescriptionKey: "Запрос не может быть пустым"])
        }
        guard status.isInteractiveAvailable else {
            let message = interactionBlockMessage
            errorMessage = message
            throw NSError(domain: "XASSAI", code: 503, userInfo: [NSLocalizedDescriptionKey: message])
        }
        guard let api else {
            markUnavailable()
            throw NSError(domain: "XASSAI", code: 503, userInfo: [NSLocalizedDescriptionKey: errorMessage ?? "XASS AI недоступен."])
        }

        let targetSessionID: String
        if let sessionID {
            targetSessionID = sessionID
        } else if let activeSessionID {
            targetSessionID = activeSessionID
        } else {
            targetSessionID = (try await createRemoteSession()).id
        }
        if activeSessionID != targetSessionID {
            activeSessionID = targetSessionID
        }

        isThinking = true
        currentReasoningStep = "Ожидание ответа XASS AI"
        let sessionTitle = sessions.first(where: { $0.id == targetSessionID })?.title ?? "XASS AI"
        HermesLiveActivityManager.shared.startActivity(
            sessionID: targetSessionID,
            sessionTitle: sessionTitle,
            prompt: trimmed
        )
        defer {
            isThinking = false
            currentReasoningStep = ""
        }

        do {
            let payload: [String: Any] = [
                "prompt": trimmed,
                "session_id": targetSessionID
            ]
            let result = try await api.request("/api/mini/hermes/chat", method: "POST", body: payload)
            guard result["ok"] as? Bool == true,
                  (result["mock"] as? Bool) != true else {
                throw NSError(domain: "XASSAI", code: 503, userInfo: [NSLocalizedDescriptionKey: "XASS AI сейчас не в сети."])
            }
            let answer = ((result["response"] as? String) ?? (result["reply"] as? String) ?? (result["text"] as? String) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !answer.isEmpty else {
                throw NSError(domain: "XASSAI", code: 502, userInfo: [NSLocalizedDescriptionKey: "XASS AI вернул пустой ответ."])
            }

            let rawTokens = (result["tokens_used"] as? Int) ?? (result["tokens"] as? Int)
            let tokens = rawTokens.flatMap { $0 >= 0 ? $0 : nil }
            let userMessage = HermesMessage(role: .user, content: trimmed, timestamp: Date())
            let assistantMessage = HermesMessage(
                role: .assistant,
                content: answer,
                timestamp: Date(),
                isReasoning: false,
                reasoningSteps: [],
                reasoningContent: nil,
                tokensUsed: tokens,
                isOfflineFallback: false
            )
            appendMessage(userMessage, for: targetSessionID)
            appendMessage(assistantMessage, for: targetSessionID)
            errorMessage = nil
            HermesLiveActivityManager.shared.endActivity(finalResponse: answer, tokens: tokens ?? 0)
            return assistantMessage
        } catch {
            markUnavailable()
            HermesLiveActivityManager.shared.failActivity()
            throw NSError(domain: "XASSAI", code: 503, userInfo: [NSLocalizedDescriptionKey: errorMessage ?? "XASS AI недоступен."])
        }
    }

    private func appendMessage(_ message: HermesMessage, for sessionID: String) {
        var list = allSessionMessages[sessionID] ?? []
        list.append(message)
        allSessionMessages[sessionID] = list

        if activeSessionID == sessionID {
            messages = list
        }

        if let index = sessions.firstIndex(where: { $0.id == sessionID }) {
            sessions[index].messageCount = list.count
            sessions[index].lastMessagePreview = String(message.content.prefix(50))
            sessions[index].updatedAt = Date()
        }
    }

}
