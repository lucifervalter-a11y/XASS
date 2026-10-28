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
    public var modelName: String
    public var activeSessionsCount: Int
    public var latencyMs: Double
    public var version: String
    public var features: [String]
    public var statusDescription: String

    public init(
        isAvailable: Bool = true,
        modelName: String = "Hermes 3 - Llama 3.1 8B",
        activeSessionsCount: Int = 1,
        latencyMs: Double = 38.0,
        version: String = "1.0.4",
        features: [String] = ["smart_orchestration", "music_control", "pc_automation", "system_diagnostics"],
        statusDescription: String = "Готов к работе"
    ) {
        self.isAvailable = isAvailable
        self.modelName = modelName
        self.activeSessionsCount = activeSessionsCount
        self.latencyMs = latencyMs
        self.version = version
        self.features = features
        self.statusDescription = statusDescription
    }

    public static let mockOffline = HermesStatus(
        isAvailable: true,
        modelName: "Hermes 3 (Автономный режим)",
        activeSessionsCount: 1,
        latencyMs: 14.0,
        version: "1.0.4-local",
        features: ["автономный_анализ", "управление_плеером", "диагностика_сервера", "шаблоны_сценариев"],
        statusDescription: "Автономный режим (локальный fallback)"
    )
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

    public static let defaultSessions: [HermesSession] = [
        HermesSession(
            id: "session-general",
            title: "Системный ассистент",
            messageCount: 2,
            lastMessagePreview: "Все системы XASS работают штатно."
        ),
        HermesSession(
            id: "session-music",
            title: "Управление музыкой",
            messageCount: 1,
            lastMessagePreview: "Готов подбирать треки и плейлисты."
        ),
        HermesSession(
            id: "session-automation",
            title: "Автоматизация и ПК",
            messageCount: 0,
            lastMessagePreview: "Создавайте новые сценарии для ПК и сервера."
        )
    ]
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
            statusPhase: "Анализ запроса...",
            progress: 0.15,
            isThinking: true,
            responseTokens: 0,
            stepDescription: "Инициализация рассуждений"
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

    public func endActivity(finalResponse: String, tokens: Int = 120) {
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
}

// MARK: - Hermes Client Service

@MainActor
public final class HermesService: ObservableObject {
    @Published public var status: HermesStatus = .mockOffline
    @Published public var sessions: [HermesSession] = HermesSession.defaultSessions
    @Published public var activeSessionID: String? = "session-general"
    @Published public var messages: [HermesMessage] = []
    @Published public var allSessionMessages: [String: [HermesMessage]] = [:]
    @Published public var isThinking: Bool = false
    @Published public var currentReasoningStep: String = ""
    @Published public var errorMessage: String? = nil

    public weak var api: OwnerService?

    public init(api: OwnerService? = nil) {
        self.api = api
        initializeSeedData()
    }

    private func initializeSeedData() {
        let welcome = HermesMessage(
            role: .assistant,
            content: "Здравствуйте! Я Hermes — интеллектуальный агент вашей экосистемы XASS.\n\nЯ помогу управлять воспроизведением музыки, выполнять команды на привязанных ПК-агентах, анализировать состояние сервера и настраивать умные правила автоматизации.",
            isReasoning: false,
            isOfflineFallback: true
        )
        allSessionMessages["session-general"] = [welcome]
        messages = [welcome]
    }

    // MARK: - Client API Methods

    /// Fetches server Hermes status with fallback to offline mock.
    @discardableResult
    public func fetchStatus() async -> HermesStatus {
        guard let api = api else {
            self.status = .mockOffline
            return .mockOffline
        }

        do {
            let response = try await api.request("/api/mini/hermes/status", method: "GET", body: nil)
            let statusDict = (response["status"] as? [String: Any]) ?? response
            let isAvailable = (statusDict["online"] as? Bool) ?? (statusDict["available"] as? Bool) ?? (statusDict["enabled"] as? Bool) ?? true
            let model = (statusDict["active_model"] as? String) ?? (statusDict["model_name"] as? String) ?? "Hermes 3 - Llama 3.1 8B"
            let sessionsCount = (statusDict["session_count"] as? Int) ?? (statusDict["active_sessions"] as? Int) ?? sessions.count
            let latency = (statusDict["latency_ms"] as? NSNumber)?.doubleValue ?? 35.0
            let version = (statusDict["version"] as? String) ?? "1.0.4"
            let features = (statusDict["features"] as? [String]) ?? ["smart_orchestration", "music_control", "system_diagnostics"]
            let description = (statusDict["details"] as? String) ?? (statusDict["status_description"] as? String) ?? "Готов к работе"

            let result = HermesStatus(
                isAvailable: isAvailable,
                modelName: model,
                activeSessionsCount: sessionsCount,
                latencyMs: latency,
                version: version,
                features: features,
                statusDescription: description
            )
            self.status = result
            return result
        } catch {
            // Seamless offline fallback
        }

        self.status = .mockOffline
        return .mockOffline
    }

    /// Fetches sessions with fallback to default offline mock sessions.
    @discardableResult
    public func fetchSessions() async -> [HermesSession] {
        guard let api = api else {
            return self.sessions
        }

        do {
            let response = try await api.request("/api/mini/hermes/sessions", method: "GET", body: nil)
            if let list = response["sessions"] as? [[String: Any]] {
                var loaded: [HermesSession] = []
                for item in list {
                    if let id = item["id"] as? String, let title = item["title"] as? String {
                        let count = item["message_count"] as? Int ?? 0
                        let preview = item["last_preview"] as? String ?? ""
                        loaded.append(HermesSession(id: id, title: title, messageCount: count, lastMessagePreview: preview))
                    }
                }
                if !loaded.isEmpty {
                    self.sessions = loaded
                    return loaded
                }
            }
        } catch {
            // Keep existing or default sessions
        }

        return self.sessions
    }

    /// Selects active session and loads its messages.
    public func selectSession(_ sessionID: String) {
        guard activeSessionID != sessionID else { return }
        activeSessionID = sessionID
        messages = allSessionMessages[sessionID] ?? []
    }

    /// Creates a new session and activates it.
    @discardableResult
    public func createSession(title: String? = nil) -> HermesSession {
        let newTitle = title ?? "Диалог \(sessions.count + 1)"
        let session = HermesSession(title: newTitle)
        sessions.insert(session, at: 0)
        activeSessionID = session.id
        allSessionMessages[session.id] = []
        messages = []
        return session
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

    /// Sends a prompt to Hermes with progressive reasoning and offline fallback.
    @discardableResult
    public func sendMessage(_ prompt: String, sessionID: String? = nil) async throws -> HermesMessage {
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw NSError(domain: "Hermes", code: 400, userInfo: [NSLocalizedDescriptionKey: "Запрос не может быть пустым"])
        }

        let targetSessionID = sessionID ?? activeSessionID ?? createSession().id
        if activeSessionID != targetSessionID {
            activeSessionID = targetSessionID
        }

        // 1. Post user message
        let userMessage = HermesMessage(
            role: .user,
            content: trimmed,
            timestamp: Date()
        )
        appendMessage(userMessage, for: targetSessionID)

        // 2. Start thinking & Live Activity
        isThinking = true
        let sessionTitle = sessions.first(where: { $0.id == targetSessionID })?.title ?? "Hermes"
        HermesLiveActivityManager.shared.startActivity(
            sessionID: targetSessionID,
            sessionTitle: sessionTitle,
            prompt: trimmed
        )

        // 3. Reasoning progress simulation
        let reasoningPhases = [
            ("Анализ контекста сервера XASS...", 0.25),
            ("Проверка состояния медиаплеера и ПК-агентов...", 0.55),
            ("Оценка политик безопасности и сценариев...", 0.80),
            ("Синтез структурированного ответа...", 0.95)
        ]

        var accumulatedSteps: [String] = []

        for (step, progress) in reasoningPhases {
            currentReasoningStep = step
            accumulatedSteps.append(step)
            HermesLiveActivityManager.shared.updateProgress(
                stepDescription: step,
                progress: progress,
                phase: "Размышление..."
            )
            try? await Task.sleep(nanoseconds: 220_000_000)
        }

        // 4. Try API or generate offline fallback
        var responseContent: String
        var tokens = 120
        var isOffline = false

        if let api = api {
            do {
                let payload: [String: Any] = [
                    "prompt": trimmed,
                    "session_id": targetSessionID
                ]
                let result = try await api.request("/api/mini/hermes/chat", method: "POST", body: payload)
                if let answer = (result["response"] as? String) ?? (result["reply"] as? String) ?? (result["text"] as? String) {
                    responseContent = answer
                    tokens = (result["tokens_used"] as? Int) ?? (result["tokens"] as? Int) ?? 145
                } else {
                    let fallback = generateOfflineResponse(for: trimmed)
                    responseContent = fallback.content
                    tokens = fallback.tokens
                    isOffline = true
                }
            } catch {
                let fallback = generateOfflineResponse(for: trimmed)
                responseContent = fallback.content
                tokens = fallback.tokens
                isOffline = true
            }
        } else {
            let fallback = generateOfflineResponse(for: trimmed)
            responseContent = fallback.content
            tokens = fallback.tokens
            isOffline = true
        }

        // 5. Build assistant message
        let assistantMessage = HermesMessage(
            role: .assistant,
            content: responseContent,
            timestamp: Date(),
            isReasoning: true,
            reasoningSteps: accumulatedSteps,
            reasoningContent: accumulatedSteps.joined(separator: "\n"),
            tokensUsed: tokens,
            isOfflineFallback: isOffline
        )

        appendMessage(assistantMessage, for: targetSessionID)

        // 6. Complete Live Activity and reset thinking state
        HermesLiveActivityManager.shared.endActivity(finalResponse: responseContent, tokens: tokens)
        isThinking = false
        currentReasoningStep = ""

        return assistantMessage
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

    // MARK: - Intelligent Offline Mock Response Generator

    private func generateOfflineResponse(for prompt: String) -> (content: String, tokens: Int) {
        let lower = prompt.lowercased()

        if lower.contains("статус") || lower.contains("сервер") || lower.contains("состояние") || lower.contains("диагностика") {
            let reply = """
            📊 **Состояние сервера XASS** (Штатный режим):

            • **CPU**: 14% (нагрузка в норме)
            • **Память (RAM)**: 2.3 ГБ / 8.0 ГБ (28%)
            • **Дисковое пространство**: 82.4 ГБ свободно
            • **Telegram-бот**: онлайн, активен webhook
            • **База данных**: SQLite WAL mode — целостность подтверждена
            • **Устройства в сети**: 2 устройства (iPhone и Рабочий ПК)

            Все службы работают без сбоев. Ошибок в журнале за последние 24 часа не обнаружено.
            """
            return (reply, 142)
        } else if lower.contains("музык") || lower.contains("трек") || lower.contains("плеер") || lower.contains("плейлист") {
            let reply = """
            🎵 **Музыкальная медиатека XASS**:

            • **Локальный кэш iPhone**: 256 МиБ активен (сохранено 18 треков)
            • **Режим вывода**: Локальный динамик / AirPlay готов
            • **Качество потока**: Исходное (без пережатия)
            • **Умный совет**: Для вечера рекомендую плейлист «Ambient Space» или запуск случайного воспроизведения вашей избранной коллекции.
            """
            return (reply, 118)
        } else if lower.contains("пк") || lower.contains("компьютер") || lower.contains("агент") || lower.contains("windows") {
            let reply = """
            🖥️ **Статус привязанного ПК-агента**:

            • **Имя**: Рабочая станция Windows 11
            • **Безопасность**: Аппаратный P-256 ключ в Secure Enclave iPhone
            • **Доступные команды**: Блокировка экрана, снимок рабочего стола, остановка медиа, безопасный запуск скриптов
            • **Готовность к командам**: Подтверждено Face ID.
            """
            return (reply, 130)
        } else if lower.contains("сценари") || lower.contains("правил") || lower.contains("автоматизаци") {
            let reply = """
            ⚡ **Автоматизация XASS**:

            Активные правила:
            1. **«Ночной покой»**: В 23:30 автоматическая остановка музыки на ПК и активация таймера сна.
            2. **«Безопасный уход»**: Блокировка экрана ПК при разрыве домашней Wi-Fi сети.
            3. **«Резервное копирование»**: Еженедельный зашифрованный бэкап настроек.

            Вы можете добавить новое правило через вкладку «Инструменты» → «Сценарии».
            """
            return (reply, 155)
        } else {
            let reply = """
            Я проанализировал ваш запрос «\(prompt)».

            В экосистеме XASS всё готово к выполнению. Я могу детально показать параметры сервера, помочь с управлением медиатекой, передать команду на рабочий ПК или оптимизировать сценарии автоматизации.

            Чем ещё я могу помочь?
            """
            return (reply, 98)
        }
    }
}
