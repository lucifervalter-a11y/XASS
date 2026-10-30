import SwiftUI

/// Quick prompt suggestion chip model.
struct HermesQuickPrompt: Identifiable {
    let id = UUID()
    let icon: String
    let title: String
    let prompt: String
}

@MainActor
public struct HermesView: View {
    @ObservedObject public var hermes: HermesService
    @ObservedObject public var themeManager = ThemeManager.shared
    weak var store: NativeStore?

    @State private var inputPrompt: String = ""
    @State private var showSessionSheet = false
    @State private var showClearConfirm = false
    @State private var copiedMessageID: String? = nil
    @State private var isCreatingSession = false
    @FocusState private var isInputFocused: Bool

    init(hermes: HermesService, store: NativeStore? = nil) {
        self.hermes = hermes
        self.store = store
    }

    private var currentTheme: AppTheme {
        themeManager.currentTheme
    }

    private let quickPrompts: [HermesQuickPrompt] = [
        HermesQuickPrompt(icon: "server.rack", title: "Статус сервера", prompt: "Какой текущий статус сервера XASS и ресурсов?"),
        HermesQuickPrompt(icon: "waveform", title: "Музыка", prompt: "Покажи состояние музыкального плеера и библиотеки."),
        HermesQuickPrompt(icon: "desktopcomputer", title: "ПК-агент", prompt: "Проверь статус привязанного ПК-агента и доступные команды."),
        HermesQuickPrompt(icon: "bolt.fill", title: "Автоматизация", prompt: "Какие сценарии автоматизации сейчас активны?"),
        HermesQuickPrompt(icon: "shield.checkerboard", title: "Безопасность", prompt: "Проведи аудит безопасности ключей и сессий."),
        HermesQuickPrompt(icon: "sparkles", title: "Совет дня", prompt: "Посоветуй оптимальные настройки медиатеки и кэша для iPhone.")
    ]

    public var body: some View {
        NavigationStack {
            ZStack {
                currentTheme.backgroundColor.ignoresSafeArea()

                VStack(spacing: 0) {
                    sessionHeaderBar

                    if let errorMessage = hermes.errorMessage {
                        availabilityErrorBanner(errorMessage)
                    }

                    if hermes.isThinking {
                        requestProgressBanner
                            .transition(.move(edge: .top).combined(with: .opacity))
                    }

                    // Messages Area
                    ScrollViewReader { proxy in
                        ScrollView {
                            LazyVStack(spacing: 16) {
                                if hermes.messages.isEmpty {
                                    welcomePlaceholder
                                } else {
                                    ForEach(hermes.messages) { message in
                                        HermesMessageRow(
                                            message: message,
                                            theme: currentTheme,
                                            copiedID: $copiedMessageID,
                                            onRetry: { prompt in
                                                inputPrompt = prompt
                                                sendMessage()
                                            }
                                        )
                                        .id(message.id)
                                    }
                                }
                            }
                            .padding(.horizontal, 16)
                            .padding(.top, 14)
                            .padding(.bottom, 12)
                        }
                        .scrollDismissesKeyboard(.interactively)
                        .onChange(of: hermes.messages.count) { _, _ in
                            if let lastID = hermes.messages.last?.id {
                                withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) {
                                    proxy.scrollTo(lastID, anchor: .bottom)
                                }
                            }
                        }
                    }

                    // Quick Chips & Input Bar
                    VStack(spacing: 10) {
                        if hermes.status.isInteractiveAvailable {
                            quickChipsView
                        }
                        inputBar
                    }
                    .padding(.bottom, 8)
                    .background(currentTheme.backgroundColor.opacity(0.95))
                }
            }
            .navigationTitle("XASS AI")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    statusPill
                }

                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button {
                            createNewSession()
                        } label: {
                            Label("Новый диалог", systemImage: "plus.bubble")
                        }
                        .disabled(!canCreateSession)

                        Button {
                            showSessionSheet = true
                        } label: {
                            Label("Все диалоги (\(hermes.sessions.count))", systemImage: "list.bullet")
                        }

                        Divider()

                        Button(role: .destructive) {
                            showClearConfirm = true
                        } label: {
                            Label("Очистить экран чата", systemImage: "trash")
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                            .font(.system(size: 17, weight: .medium))
                            .foregroundStyle(currentTheme.accentColor)
                    }
                    .accessibilityLabel("Меню управления диалогами XASS AI")
                }
            }
            .confirmationDialog(
                "Убрать сообщения текущего диалога с этого экрана? История на сервере не изменится.",
                isPresented: $showClearConfirm,
                titleVisibility: .visible
            ) {
                Button("Очистить на iPhone", role: .destructive) {
                    withAnimation {
                        hermes.clearActiveHistory()
                    }
                }
                Button("Отмена", role: .cancel) {}
            }
            .sheet(isPresented: $showSessionSheet) {
                sessionSelectorSheet
            }
            .task {
                let status = await hermes.fetchStatus()
                if status.isInteractiveAvailable {
                    await hermes.fetchSessions()
                }
            }
        }
    }

    // MARK: - Header & Status

    private var statusAccessibilityLabel: String {
        var label: String
        if hermes.status.isInteractiveAvailable {
            label = "Статус XASS AI: интерактивный режим доступен"
        } else if hermes.status.isAvailable {
            label = "Статус XASS AI: среда онлайн, интерактивный режим недоступен"
        } else {
            label = "Статус XASS AI: офлайн"
        }
        if !hermes.status.version.isEmpty {
            label += ", версия \(hermes.status.version)"
        }
        return label
    }

    private var statusColor: Color {
        if hermes.status.isInteractiveAvailable {
            return .green
        }
        return hermes.status.isAvailable ? .yellow : .orange
    }

    private var statusPill: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
                .shadow(color: hermes.status.isInteractiveAvailable ? Color.green.opacity(0.6) : Color.clear, radius: 4)

            Text("XASS AI")
                .font(.caption.weight(.bold))
                .foregroundStyle(.white)

            if !hermes.status.version.isEmpty {
                Text(hermes.status.version)
                    .font(.caption2)
                    .foregroundStyle(currentTheme.secondaryTextColor)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(currentTheme.cardColor, in: Capsule())
        .overlay(Capsule().stroke(currentTheme.borderColor, lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(statusAccessibilityLabel)
    }

    private var sessionHeaderBar: some View {
        HStack(spacing: 10) {
            let currentTitle = hermes.sessions.first(where: { $0.id == hermes.activeSessionID })?.title ?? "Основной чат"

            Button {
                showSessionSheet = true
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "bubble.left.and.bubble.right.fill")
                        .font(.caption)
                        .foregroundStyle(currentTheme.accentColor)

                    Text(currentTitle)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.white)
                        .lineLimit(1)

                    Image(systemName: "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(currentTheme.secondaryTextColor)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(currentTheme.cardColor, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(currentTheme.borderColor, lineWidth: 0.8))
            }
            .accessibilityLabel("Текущий диалог: \(currentTitle). Нажмите для переключения.")

            Spacer()

            Button {
                createNewSession()
            } label: {
                Image(systemName: "plus")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.white)
                    .frame(width: 30, height: 30)
                    .background(currentTheme.accentColor, in: Circle())
            }
            .accessibilityLabel("Создать новый диалог")
            .disabled(!canCreateSession)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
    }

    // MARK: - Request Progress

    private var requestProgressBanner: some View {
        HStack(spacing: 12) {
            ZStack {
                Circle()
                    .fill(currentTheme.accentColor.opacity(0.2))
                    .frame(width: 32, height: 32)
                Image(systemName: "sparkles")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(currentTheme.accentColor)
            }

            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text("XASS AI обрабатывает запрос…")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.white)

                    Spacer()

                    ProgressView()
                        .scaleEffect(0.7)
                        .tint(currentTheme.accentColor)
                }

                Text(hermes.currentReasoningStep.isEmpty ? "Ожидание ответа" : hermes.currentReasoningStep)
                    .font(.caption2)
                    .foregroundStyle(currentTheme.secondaryTextColor)
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 14)
                .fill(currentTheme.cardColor)
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(currentTheme.accentColor.opacity(0.4), lineWidth: 1))
                .shadow(color: currentTheme.glowingTintColor, radius: 8)
        )
        .padding(.horizontal, 16)
        .padding(.bottom, 6)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("XASS AI обрабатывает запрос: \(hermes.currentReasoningStep)")
    }

    private func availabilityErrorBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(Color.orange)

            Text(message)
                .font(.caption)
                .foregroundStyle(.white)
                .textSelection(.enabled)

            Spacer(minLength: 0)

            Button("Повторить") {
                Task {
                    let status = await hermes.fetchStatus()
                    if status.isInteractiveAvailable {
                        await hermes.fetchSessions()
                    }
                }
            }
            .font(.caption.weight(.semibold))
            .foregroundStyle(currentTheme.accentColor)
            .accessibilityHint("Повторно проверить доступность XASS AI")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color.orange.opacity(0.14), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.orange.opacity(0.45), lineWidth: 1))
        .padding(.horizontal, 16)
        .padding(.bottom, 6)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("XASS AI: \(message)")
    }

    // MARK: - Welcome State

    private var welcomePlaceholder: some View {
        VStack(spacing: 16) {
            ZStack {
                Circle()
                    .fill(currentTheme.cardColor)
                    .frame(width: 80, height: 80)
                    .overlay(Circle().stroke(currentTheme.borderColor, lineWidth: 1.5))
                    .shadow(color: currentTheme.glowingTintColor, radius: 16)

                Image(systemName: "sparkles")
                    .font(.system(size: 36, weight: .semibold))
                    .foregroundStyle(currentTheme.accentColor)
            }
            .padding(.top, 24)

            VStack(spacing: 6) {
                Text("XASS AI")
                    .font(.title2.weight(.bold))
                    .foregroundStyle(.white)

                Text(
                    hermes.status.isInteractiveAvailable
                        ? "Введите запрос. Ответ появится после подтверждённого ответа сервиса."
                        : (hermes.errorMessage ?? hermes.status.statusDescription)
                )
                    .font(.subheadline)
                    .foregroundStyle(currentTheme.secondaryTextColor)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
            }

            if hermes.status.isInteractiveAvailable {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Популярные запросы:")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(currentTheme.secondaryTextColor)
                        .padding(.horizontal, 4)

                    ForEach(quickPrompts.prefix(3)) { chip in
                        Button {
                            inputPrompt = chip.prompt
                            sendMessage()
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: chip.icon)
                                    .font(.body)
                                    .foregroundStyle(currentTheme.accentColor)
                                    .frame(width: 24)

                                VStack(alignment: .leading, spacing: 2) {
                                    Text(chip.title)
                                        .font(.subheadline.weight(.medium))
                                        .foregroundStyle(.white)

                                    Text(chip.prompt)
                                        .font(.caption)
                                        .foregroundStyle(currentTheme.secondaryTextColor)
                                        .lineLimit(1)
                                }

                                Spacer()

                                Image(systemName: "arrow.up.right")
                                    .font(.caption2)
                                    .foregroundStyle(currentTheme.secondaryTextColor)
                            }
                            .padding(12)
                            .background(currentTheme.cardColor, in: RoundedRectangle(cornerRadius: 12))
                            .overlay(RoundedRectangle(cornerRadius: 12).stroke(currentTheme.borderColor, lineWidth: 0.8))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Отправить запрос: \(chip.title)")
                    }
                }
                .padding(.top, 10)
            }
        }
        .padding(.vertical, 16)
    }

    // MARK: - Quick Chips Bar

    private var quickChipsView: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(quickPrompts) { chip in
                    Button {
                        inputPrompt = chip.prompt
                        sendMessage()
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: chip.icon)
                                .font(.caption)
                                .foregroundStyle(currentTheme.accentColor)

                            Text(chip.title)
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.white)
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(currentTheme.cardColor, in: Capsule())
                        .overlay(Capsule().stroke(currentTheme.borderColor, lineWidth: 0.8))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Быстрый запрос: \(chip.title)")
                }
            }
            .padding(.horizontal, 16)
        }
    }

    // MARK: - Input Bar

    private var inputBar: some View {
        HStack(alignment: .bottom, spacing: 10) {
            HStack(alignment: .center, spacing: 8) {
                TextField("Введите запрос для XASS AI…", text: $inputPrompt, axis: .vertical)
                    .font(.body)
                    .lineLimit(1...5)
                    .focused($isInputFocused)
                    .foregroundStyle(.white)
                    .tint(currentTheme.accentColor)
                    .accessibilityLabel("Поле ввода запроса к XASS AI")
                    .disabled(!hermes.status.isInteractiveAvailable)

                if !inputPrompt.isEmpty {
                    Button {
                        inputPrompt = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.body)
                            .foregroundStyle(currentTheme.secondaryTextColor)
                    }
                    .accessibilityLabel("Очистить поле ввода")
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 20)
                    .fill(currentTheme.cardColor)
                    .overlay(RoundedRectangle(cornerRadius: 20).stroke(isInputFocused ? currentTheme.accentColor : currentTheme.borderColor, lineWidth: 1))
            )

            Button(action: sendMessage) {
                ZStack {
                    Circle()
                        .fill(canSend ? currentTheme.accentColor : currentTheme.cardColor)
                        .frame(width: 42, height: 42)
                        .overlay(Circle().stroke(currentTheme.borderColor, lineWidth: 1))
                        .shadow(color: canSend ? currentTheme.glowingTintColor : Color.clear, radius: 6)

                    if hermes.isThinking {
                        ProgressView()
                            .tint(.white)
                    } else {
                        Image(systemName: "arrow.up")
                            .font(.system(size: 16, weight: .bold))
                            .foregroundStyle(canSend ? .white : currentTheme.secondaryTextColor)
                    }
                }
            }
            .disabled(!canSend)
            .accessibilityLabel("Отправить запрос")
            .accessibilityHint("Дважды коснитесь, чтобы отправить введенный запрос в XASS AI")
        }
        .padding(.horizontal, 16)
    }

    private var canSend: Bool {
        hermes.status.isInteractiveAvailable
            && !inputPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !hermes.isThinking
    }

    private var canCreateSession: Bool {
        hermes.status.isInteractiveAvailable && !isCreatingSession
    }

    private func sendMessage() {
        guard canSend else { return }
        let promptToSend = inputPrompt
        inputPrompt = ""
        isInputFocused = false

        Task {
            do {
                try await hermes.sendMessage(promptToSend)
            } catch {
                hermes.errorMessage = error.localizedDescription
                if inputPrompt.isEmpty {
                    inputPrompt = promptToSend
                }
            }
        }
    }

    private func createNewSession(closeSheetOnSuccess: Bool = false) {
        guard canCreateSession else { return }
        isCreatingSession = true
        Task {
            defer { isCreatingSession = false }
            do {
                _ = try await hermes.createRemoteSession()
                if closeSheetOnSuccess {
                    showSessionSheet = false
                }
            } catch {
                hermes.errorMessage = error.localizedDescription
            }
        }
    }

    // MARK: - Sessions Sheet

    private var sessionSelectorSheet: some View {
        NavigationStack {
            List {
                Section("Активные диалоги") {
                    ForEach(hermes.sessions) { session in
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(session.title)
                                        .font(.headline)
                                        .foregroundStyle(.white)

                                    if session.id == hermes.activeSessionID {
                                        Text("Текущий")
                                            .font(.caption2.bold())
                                            .padding(.horizontal, 6)
                                            .padding(.vertical, 2)
                                            .background(currentTheme.accentColor.opacity(0.3), in: Capsule())
                                            .foregroundStyle(currentTheme.accentColor)
                                    }
                                }

                                if !session.lastMessagePreview.isEmpty {
                                    Text(session.lastMessagePreview)
                                        .font(.caption)
                                        .foregroundStyle(currentTheme.secondaryTextColor)
                                        .lineLimit(1)
                                }
                            }

                            Spacer()

                            Text("\(session.messageCount)")
                                .font(.caption.bold())
                                .foregroundStyle(currentTheme.secondaryTextColor)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(currentTheme.cardColor, in: Capsule())
                        }
                        .contentShape(Rectangle())
                        .onTapGesture {
                            hermes.selectSession(session.id)
                            showSessionSheet = false
                        }
                        .swipeActions(edge: .trailing) {
                            if hermes.sessions.count > 1 {
                                Button(role: .destructive) {
                                    hermes.deleteSession(session.id)
                                } label: {
                                    Label("Скрыть на iPhone", systemImage: "eye.slash")
                                }
                            }
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(currentTheme.backgroundColor.ignoresSafeArea())
            .navigationTitle("Диалоги XASS AI")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Готово") {
                        showSessionSheet = false
                    }
                    .foregroundStyle(currentTheme.accentColor)
                }

                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        createNewSession(closeSheetOnSuccess: true)
                    } label: {
                        Image(systemName: "plus")
                            .foregroundStyle(currentTheme.accentColor)
                    }
                    .disabled(!canCreateSession)
                }
            }
        }
    }
}

// MARK: - Message Row

struct HermesMessageRow: View {
    let message: HermesMessage
    let theme: AppTheme
    @Binding var copiedID: String?
    let onRetry: (String) -> Void

    var isUser: Bool {
        message.role == .user
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            if !isUser {
                // XASS AI avatar
                ZStack {
                    Circle()
                        .fill(theme.cardColor)
                        .frame(width: 34, height: 34)
                        .overlay(Circle().stroke(theme.borderColor, lineWidth: 1))
                        .shadow(color: theme.glowingTintColor, radius: 4)

                    Image(systemName: "sparkles")
                        .font(.system(size: 14, weight: .bold))
                        .foregroundStyle(theme.accentColor)
                }
                .padding(.top, 2)
            }

            VStack(alignment: isUser ? .trailing : .leading, spacing: 6) {
                // Bubble container
                VStack(alignment: .leading, spacing: 8) {
                    Text(message.content)
                        .font(.body)
                        .foregroundStyle(isUser ? Color.white : Color(white: 0.96))
                        .textSelection(.enabled)
                        .lineSpacing(3)

                }
                .padding(.horizontal, 15)
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(isUser ? theme.accentColor : theme.cardColor)
                        .overlay(
                            RoundedRectangle(cornerRadius: 18, style: .continuous)
                                .stroke(isUser ? Color.clear : theme.borderColor, lineWidth: 0.8)
                        )
                )
                .contextMenu {
                    Button {
                        copyMessage(message.content)
                    } label: {
                        Label("Скопировать текст", systemImage: "doc.on.doc")
                    }

                    ShareLink(item: message.content) {
                        Label("Поделиться", systemImage: "square.and.arrow.up")
                    }

                    if isUser {
                        Button {
                            onRetry(message.content)
                        } label: {
                            Label("Повторить запрос", systemImage: "arrow.clockwise")
                        }
                    }
                }

                // Action Bar (Copy button & time)
                HStack(spacing: 12) {
                    if !isUser {
                        Button {
                            copyMessage(message.content)
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: copiedID == message.id ? "checkmark" : "doc.on.doc")
                                    .font(.caption2)
                                Text(copiedID == message.id ? "Скопировано" : "Копировать")
                                    .font(.caption2)
                            }
                            .foregroundStyle(copiedID == message.id ? Color.green : theme.secondaryTextColor)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Скопировать ответ XASS AI в буфер обмена")

                        if let tokens = message.tokensUsed {
                            Text("\(tokens) токенов")
                                .font(.caption2)
                                .foregroundStyle(theme.secondaryTextColor.opacity(0.8))
                        }
                    }

                    Text(message.timestamp.formatted(date: .omitted, time: .shortened))
                        .font(.caption2)
                        .foregroundStyle(theme.secondaryTextColor.opacity(0.7))
                }
                .padding(.horizontal, 4)
            }
            .frame(maxWidth: .infinity, alignment: isUser ? .trailing : .leading)

            if isUser {
                // User avatar indicator
                ZStack {
                    Circle()
                        .fill(theme.accentColor.opacity(0.25))
                        .frame(width: 32, height: 32)
                        .overlay(Circle().stroke(theme.accentColor.opacity(0.5), lineWidth: 1))

                    Image(systemName: "person.fill")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.white)
                }
                .padding(.top, 2)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(isUser ? "Вы" : "XASS AI"): \(message.content)")
    }

    private func copyMessage(_ text: String) {
        UIPasteboard.general.string = text
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.impactOccurred()
        withAnimation {
            copiedID = message.id
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            if copiedID == message.id {
                copiedID = nil
            }
        }
    }
}
