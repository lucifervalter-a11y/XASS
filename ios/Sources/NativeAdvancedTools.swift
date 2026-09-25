import SwiftUI
import UIKit
import UniformTypeIdentifiers
import QuickLook

@MainActor struct NativeWorkspaceMenu: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        List {
            Section("Рабочая область") {
                NavigationLink { NativeTimelineView(store: store) } label: { Label("История событий", systemImage: "clock.arrow.circlepath") }
                NavigationLink { NativeRulesView(store: store) } label: { Label("Правила уведомлений", systemImage: "bell.badge") }
                NavigationLink { NativeScenariosView(store: store) } label: { Label("Сценарии", systemImage: "bolt.horizontal.circle") }
                NavigationLink { NativeConversationsView(store: store) } label: { Label("Архив переписок", systemImage: "text.bubble") }
            }
            Section("Компьютеры") {
                if store.devices.isEmpty { Text("Подключённые ПК появятся здесь после входа.").foregroundStyle(.secondary) }
                ForEach(store.devices) { device in
                    NavigationLink { NativePCWorkspace(store: store, device: device) } label: {
                        Label(device.name, systemImage: device.online ? "desktopcomputer" : "desktopcomputer.trianglebadge.exclamationmark")
                    }
                }
            }
            Section {
                NavigationLink { NativeWorkspaceKeyImport(origin: store.api.origin) } label: { Label("Ключ защищённых файлов ПК", systemImage: "key.horizontal") }
            }
        }.navigationTitle("Рабочая область")
    }
}

private struct NativeAdvancedNotice: View {
    @ObservedObject var data: NativeAdvancedStore
    var body: some View {
        if data.busy { HStack(spacing: 10) { ProgressView(); Text(data.status ?? "Загрузка…").font(.subheadline) } }
        if let error = data.error { Label(error, systemImage: "exclamationmark.circle").font(.subheadline).foregroundStyle(.red).textSelection(.enabled) }
        if !data.busy, let status = data.status { Text(status).font(.subheadline).foregroundStyle(.secondary) }
    }
}

@MainActor private struct NativeTimelineView: View {
    @ObservedObject var store: NativeStore
    @StateObject private var data: NativeAdvancedStore
    @State private var type = ""
    @State private var level = ""
    @State private var device = ""
    init(store: NativeStore) { self.store = store; _data = StateObject(wrappedValue: NativeAdvancedStore(owner: store)) }
    var body: some View {
        List {
            Section("Фильтры") {
                Picker("События", selection: $type) {
                    Text("Все").tag(""); Text("Уведомления").tag("notification"); Text("Команды").tag("command")
                    Text("Действия").tag("action"); Text("Состояние ПК").tag("agent")
                }
                Picker("Важность", selection: $level) {
                    Text("Любая").tag(""); Text("Информация").tag("info"); Text("Успешно").tag("success")
                    Text("Предупреждение").tag("warning"); Text("Критично").tag("critical")
                }
                Picker("Устройство", selection: $device) {
                    Text("Все").tag(""); ForEach(store.devices) { Text($0.name).tag($0.name) }
                }
                Button("Применить фильтры") { reload() }
            }.disabled(data.busy)
            Section {
                NativeAdvancedNotice(data: data)
                if !data.busy && data.error == nil && data.events.isEmpty { Text("Событий по выбранным фильтрам нет.").foregroundStyle(.secondary) }
                ForEach(data.events) { event in
                    VStack(alignment: .leading, spacing: 6) {
                        Label(event.title, systemImage: event.level == "critical" ? "exclamationmark.octagon" : event.level == "success" ? "checkmark.circle" : "circle.fill")
                            .font(.headline).foregroundStyle(event.level == "critical" ? Color.red : Color.primary)
                        if !event.message.isEmpty { Text(event.message).font(.subheadline).textSelection(.enabled) }
                        Text([NativeAdvancedStore.date(event.date), event.device].filter { !$0.isEmpty }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
                    }.padding(.vertical, 3)
                }
            } footer: { Text("Последние 200 событий, соответствующих фильтрам.") }
        }.navigationTitle("История событий")
            .task { await refresh() }.refreshable { await refresh() }
    }
    private func refresh() async { await data.perform { try await data.loadTimeline(type: type, level: level, device: device) } }
    private func reload() { Task { await refresh() } }
}

@MainActor private struct NativeRulesView: View {
    @ObservedObject var store: NativeStore
    @StateObject private var data: NativeAdvancedStore
    @State private var editing: NativeAutomationRule?
    @State private var deleting: NativeAutomationRule?
    init(store: NativeStore) { self.store = store; _data = StateObject(wrappedValue: NativeAdvancedStore(owner: store)) }
    var body: some View {
        List {
            NativeAdvancedNotice(data: data)
            if !data.busy && data.error == nil && data.rules.isEmpty { ContentUnavailableView("Правил пока нет", systemImage: "bell.badge", description: Text("Добавьте правило, чтобы получать уведомления о состоянии устройств.")) }
            ForEach(data.rules) { rule in
                Button { editing = rule } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(rule.name).font(.headline).foregroundStyle(.primary)
                            Text(NativeAutomationRule.label(rule.condition)).font(.subheadline).foregroundStyle(.secondary)
                            Text(rule.device.isEmpty ? "Все устройства" : rule.device).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer(); Image(systemName: rule.enabled ? "bell.fill" : "bell.slash").foregroundStyle(rule.enabled ? Color.accentColor : .secondary)
                    }
                }.swipeActions { Button("Удалить", role: .destructive) { deleting = rule } }
            }
        }.navigationTitle("Правила уведомлений")
            .toolbar { Button { editing = NativeAutomationRule() } label: { Label("Добавить правило", systemImage: "plus") }.disabled(data.busy) }
            .task { await data.perform { try await data.loadRules() } }
            .refreshable { await data.perform { try await data.loadRules() } }
            .sheet(item: $editing) { rule in NavigationStack { NativeRuleEditor(store: store, data: data, rule: rule) } }
            .confirmationDialog("Удалить правило?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                if let deleting { Button("Удалить «\(deleting.name)»", role: .destructive) { Task { await data.perform { try await data.deleteRule(deleting) } }; self.deleting = nil } }
            }
    }
}

@MainActor private struct NativeRuleEditor: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var data: NativeAdvancedStore
    @State var rule: NativeAutomationRule
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        Form {
            Section("Правило") {
                TextField("Название", text: $rule.name)
                Toggle("Включено", isOn: $rule.enabled)
                Picker("Условие", selection: $rule.condition) { ForEach(NativeAutomationRule.conditions, id: \.self) { Text(NativeAutomationRule.label($0)).tag($0) } }
                Picker("Устройство", selection: $rule.device) {
                    Text("Все устройства").tag(""); ForEach(store.devices) { Text($0.name).tag($0.name) }
                    if !rule.device.isEmpty && !store.devices.contains(where: { $0.name == rule.device }) { Text(rule.device).tag(rule.device) }
                }
                if rule.condition == "service_down" { TextField("Название сервиса (пусто = все)", text: $rule.service).textInputAutocapitalization(.never).autocorrectionDisabled() }
                if ["agent_offline", "cpu_high", "disk_low"].contains(rule.condition) {
                    HStack { Text(rule.condition == "agent_offline" ? "Нет связи, минут" : rule.condition == "cpu_high" ? "CPU, %" : "Свободно на диске, %")
                        Spacer(); TextField("Порог", value: $rule.threshold, format: .number).keyboardType(.decimalPad).multilineTextAlignment(.trailing).frame(maxWidth: 100)
                    }
                }
            }
            Section("Уведомления") {
                Stepper("Условие длится \(rule.duration) мин.", value: $rule.duration, in: 0...1440)
                Stepper("Повтор через \(rule.cooldown) мин.", value: $rule.cooldown, in: 1...10080, step: 1)
                Picker("Важность", selection: $rule.priority) {
                    Text("Информация").tag("info"); Text("Успешно").tag("success"); Text("Предупреждение").tag("warning"); Text("Критично").tag("critical")
                }
            }
            NativeAdvancedNotice(data: data)
        }.navigationTitle("Правило")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() }.disabled(data.busy) }
                ToolbarItem(placement: .confirmationAction) { Button("Сохранить") { Task { await data.perform { try await data.saveRule(rule) }; if data.error == nil { dismiss() } } }
                    .disabled(data.busy || rule.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || rule.name.count > 120 || !rule.threshold.isFinite || rule.threshold < 0 || rule.threshold > 10000) }
            }.interactiveDismissDisabled(data.busy)
    }
}

@MainActor private struct NativeScenariosView: View {
    @ObservedObject var store: NativeStore
    @StateObject private var data: NativeAdvancedStore
    @State private var editing: NativeScenario?
    @State private var running: NativeScenario?
    @State private var deleting: NativeScenario?
    init(store: NativeStore) { self.store = store; _data = StateObject(wrappedValue: NativeAdvancedStore(owner: store)) }
    var body: some View {
        List {
            NativeAdvancedNotice(data: data)
            ForEach(data.scenarios) { scenario in
                VStack(alignment: .leading, spacing: 9) {
                    HStack { Text(scenario.name).font(.headline); Spacer(); if !scenario.enabled { Text("Выключен").font(.caption).foregroundStyle(.secondary) } }
                    Text(scenario.actions.map(NativeScenario.label).joined(separator: " → ")).font(.subheadline).foregroundStyle(.secondary)
                    Text(scenario.devices.isEmpty ? "Все подключённые ПК" : scenario.devices.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                    if !scenario.schedule.isEmpty { Label("Ежедневно в \(scenario.schedule) по времени сервера", systemImage: "clock").font(.caption) }
                    HStack {
                        Button { running = scenario } label: { Label("Запустить", systemImage: "play.fill") }.buttonStyle(.borderedProminent).disabled(!scenario.enabled || data.busy || store.busy)
                        if !scenario.builtin { Button("Изменить") { editing = scenario }.buttonStyle(.bordered).disabled(data.busy) }
                    }
                }.padding(.vertical, 4).swipeActions {
                    if !scenario.builtin { Button("Удалить", role: .destructive) { deleting = scenario } }
                }
            }
        }.navigationTitle("Сценарии")
            .toolbar { Button { editing = NativeScenario() } label: { Label("Добавить сценарий", systemImage: "plus") }.disabled(data.busy) }
            .task { await data.perform { try await data.loadScenarios() } }
            .refreshable { await data.perform { try await data.loadScenarios() } }
            .sheet(item: $editing) { scenario in NavigationStack { NativeScenarioEditor(store: store, data: data, scenario: scenario) } }
            .confirmationDialog("Запустить сценарий?", isPresented: Binding(get: { running != nil }, set: { if !$0 { running = nil } }), titleVisibility: .visible) {
                if let running { Button("Запустить «\(running.name)»") { Task { await data.perform { try await data.runScenario(running) } }; self.running = nil } }
            } message: { if let running { Text(running.actions.map(NativeScenario.label).joined(separator: "\n")) } }
            .confirmationDialog("Удалить сценарий?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                if let deleting { Button("Удалить «\(deleting.name)»", role: .destructive) { Task { await data.perform { try await data.deleteScenario(deleting) } }; self.deleting = nil } }
            }
    }
}

@MainActor private struct NativeScenarioEditor: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var data: NativeAdvancedStore
    @State var scenario: NativeScenario
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        Form {
            Section("Сценарий") {
                TextField("Название", text: $scenario.name)
                Toggle("Включён", isOn: $scenario.enabled)
                TextField("Ежедневно в ЧЧ:ММ (необязательно)", text: $scenario.schedule).keyboardType(.numbersAndPunctuation)
                Text("Время сервера. Пустое поле означает запуск вручную.").font(.caption).foregroundStyle(.secondary)
                Stepper("Задержка команд между шагами: \(scenario.delay) с", value: $scenario.delay, in: 0...3600, step: 5)
            }
            Section("Действия по порядку") {
                ForEach(Array(scenario.actions.enumerated()), id: \.element) { index, action in
                    HStack {
                        Text("\(index + 1). \(NativeScenario.label(action))")
                        Spacer()
                        Button { scenario.actions.remove(at: index) } label: { Image(systemName: "minus.circle.fill").foregroundStyle(.red) }.buttonStyle(.borderless).accessibilityLabel("Убрать действие")
                    }
                }.onMove { scenario.actions.move(fromOffsets: $0, toOffset: $1) }
                Menu("Добавить действие") {
                    ForEach(NativeScenario.availableActions.filter { !scenario.actions.contains($0) }, id: \.self) { action in
                        Button(NativeScenario.label(action)) { scenario.actions.append(action) }
                    }
                }
            }
            Section("Устройства") {
                Text(scenario.devices.isEmpty ? "Будут использованы все ПК, подключённые в момент запуска." : "Будут использованы выбранные ПК, если они в сети.").font(.caption).foregroundStyle(.secondary)
                ForEach(store.devices) { device in
                    Toggle(device.name, isOn: Binding(get: { scenario.devices.contains(device.name) }, set: { selected in
                        if selected { scenario.devices.append(device.name) } else { scenario.devices.removeAll { $0 == device.name } }
                    }))
                }
                ForEach(scenario.devices.filter { name in !store.devices.contains { $0.name == name } }, id: \.self) { name in
                    Button("Убрать недоступный ПК: \(name)") { scenario.devices.removeAll { $0 == name } }
                }
            }
            NativeAdvancedNotice(data: data)
        }.navigationTitle("Сценарий")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() }.disabled(data.busy) }
                ToolbarItem(placement: .primaryAction) { EditButton() }
                ToolbarItem(placement: .confirmationAction) { Button("Сохранить") { Task { await data.perform { try await data.saveScenario(scenario) }; if data.error == nil { dismiss() } } }.disabled(data.busy || !scenario.valid || scenario.name.count > 120) }
            }.interactiveDismissDisabled(data.busy)
    }
}

@MainActor private struct NativeConversationsView: View {
    @ObservedObject var store: NativeStore
    @StateObject private var data: NativeAdvancedStore
    @State private var query = ""
    init(store: NativeStore) { self.store = store; _data = StateObject(wrappedValue: NativeAdvancedStore(owner: store)) }
    var body: some View {
        List {
            NativeAdvancedNotice(data: data)
            if !data.busy && data.error == nil && data.chats.isEmpty { ContentUnavailableView("Архив пока пуст", systemImage: "text.bubble", description: Text("Здесь появятся переписки, сохранённые ботом XASS.")) }
            ForEach(data.chats.filter { query.isEmpty || $0.title.localizedCaseInsensitiveContains(query) || $0.text.localizedCaseInsensitiveContains(query) }) { chat in
                NavigationLink { NativeConversationReader(store: store, chat: chat) } label: {
                    VStack(alignment: .leading, spacing: 5) {
                        HStack { if chat.pinned { Image(systemName: "pin.fill").font(.caption) }; Text(chat.title).font(.headline) }
                        Text(chat.text).lineLimit(2).font(.subheadline).foregroundStyle(.secondary)
                        Text("Сообщений: \(chat.count)").font(.caption).foregroundStyle(.secondary)
                    }
                }.swipeActions(edge: .leading) {
                    Button(chat.pinned ? "Открепить" : "Закрепить") { Task { await data.perform { try await data.pin(chat) } } }.tint(.orange)
                }
            }
        }.navigationTitle("Архив переписок").searchable(text: $query, prompt: "Название или последнее сообщение")
            .task { await data.perform { try await data.loadChats() } }
            .refreshable { await data.perform { try await data.loadChats() } }
    }
}

@MainActor private struct NativeConversationReader: View {
    @ObservedObject var store: NativeStore
    let chat: NativeConversation
    @StateObject private var data: NativeAdvancedStore
    @State private var filter = ""
    init(store: NativeStore, chat: NativeConversation) { self.store = store; self.chat = chat; _data = StateObject(wrappedValue: NativeAdvancedStore(owner: store)) }
    var body: some View {
        List {
            Section {
                Picker("Показать", selection: $filter) {
                    Text("Все").tag(""); Text("Удалённые").tag("deleted"); Text("Изменённые").tag("edited"); Text("С медиа").tag("media")
                }.disabled(data.busy)
                NativeAdvancedNotice(data: data)
                if data.hasMore { Button("Загрузить более ранние сообщения") { Task { await data.perform { try await data.loadMessages(chat: chat.id, filter: filter, earlier: true) } } }.disabled(data.busy) }
            }
            if !data.busy && data.error == nil && data.messages.isEmpty { Text("Сообщений по этому фильтру нет.").foregroundStyle(.secondary) }
            ForEach(data.messages) { message in
                VStack(alignment: .leading, spacing: 8) {
                    HStack { Text(message.sender.isEmpty ? "Сообщение" : message.sender).font(.headline); Spacer(); if message.deleted { Label("Удалено", systemImage: "trash").font(.caption).foregroundStyle(.red) } }
                    if let reply = message.reply { Text("Ответ: " + reply).font(.caption).foregroundStyle(.secondary).padding(.leading, 8) }
                    if !message.text.isEmpty { Text(message.text).textSelection(.enabled) }
                    ForEach(Array(message.media.enumerated()), id: \.offset) { _, media in
                        Button { Task { await data.perform { try await data.previewAttachment(media) } } } label: { Label("Открыть вложение: " + (media["type"] as? String ?? "файл"), systemImage: "paperclip") }.disabled(data.busy)
                    }
                    if !message.revisions.isEmpty {
                        DisclosureGroup("История изменений (\(message.revisions.count))") {
                            ForEach(Array(message.revisions.enumerated()), id: \.offset) { _, revision in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(revision["text"] as? String ?? "").textSelection(.enabled)
                                    Text(NativeAdvancedStore.date(revision["date"] as? String ?? "")).font(.caption).foregroundStyle(.secondary)
                                }.padding(.vertical, 4)
                            }
                        }.font(.subheadline)
                    }
                    Text(NativeAdvancedStore.date(message.date) + (message.edited ? " · изменено" : "")).font(.caption).foregroundStyle(.secondary)
                }.padding(.vertical, 5)
            }
        }.navigationTitle(chat.title).navigationBarTitleDisplayMode(.inline)
            .task { await refresh() }.refreshable { await refresh() }
            .onChange(of: filter) { _, _ in Task { await refresh() } }
            .quickLookPreview($data.previewURL)
            .onDisappear { data.clearPreviews() }
    }
    private func refresh() async { await data.perform { try await data.loadMessages(chat: chat.id, filter: filter) } }
}

@MainActor struct NativePCWorkspace: View {
    @ObservedObject var store: NativeStore
    let device: NativeDevice
    @StateObject private var data: NativeAdvancedStore
    @State private var root = "downloads"
    @State private var clipboardDraft = ""
    @State private var confirmClipboard = false
    @State private var deleting: NativeRemoteFileTarget?
    @State private var selectingUpload = false
    @State private var uploadRoot = "downloads"
    @State private var uploadPath = ""
    @State private var commandTask: Task<Void, Never>?
    init(store: NativeStore, device: NativeDevice) { self.store = store; self.device = device; _data = StateObject(wrappedValue: NativeAdvancedStore(owner: store)) }
    var body: some View {
        List {
            Section {
                NativeAdvancedNotice(data: data)
                if !(store.devices.first { $0.id == device.id }?.online ?? device.online) { Label("ПК не в сети. Команда будет ожидать подключения.", systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
            }
            Section("Экран") {
                Button { execute { try await data.capture(device) } } label: { Label("Получить новый снимок", systemImage: "camera.viewfinder") }.disabled(data.busy)
                if let screenshot = data.screenshot { Image(uiImage: screenshot).resizable().scaledToFit().accessibilityLabel("Снимок экрана \(device.name)") }
            }
            Section("Файлы ПК") {
                Picker("Папка", selection: $root) {
                    Text("Загрузки").tag("downloads"); Text("Рабочий стол").tag("desktop"); Text("Документы").tag("documents"); Text("Файлы XASS").tag("xass_files")
                }.disabled(data.busy)
                Button("Открыть папку") { execute { try await data.listFiles(device, root: root) } }.disabled(data.busy)
                if data.filesLoaded {
                    Text(data.currentRoot + "/" + data.currentPath).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                    Button { uploadRoot = data.currentRoot; uploadPath = data.currentPath; selectingUpload = true } label: { Label("Загрузить файл в эту папку", systemImage: "square.and.arrow.up") }.disabled(data.busy)
                    if !data.currentPath.isEmpty {
                        Button { let parent = data.currentPath.split(separator: "/").dropLast().joined(separator: "/"); execute { try await data.listFiles(device, root: data.currentRoot, path: parent) } } label: { Label("На уровень выше", systemImage: "arrow.up") }.disabled(data.busy)
                    }
                    if data.files.isEmpty { Text("Папка пуста").foregroundStyle(.secondary) }
                    ForEach(data.files) { file in
                        Button {
                            if file.directory {
                                let path = data.currentPath.isEmpty ? file.name : data.currentPath + "/" + file.name
                                execute { try await data.listFiles(device, root: data.currentRoot, path: path) }
                            } else { execute { try await data.download(device, file: file) } }
                        } label: {
                            HStack { Label(file.name, systemImage: file.directory ? "folder" : "doc"); Spacer(); if !file.directory { Text(ByteCountFormatter.string(fromByteCount: file.size, countStyle: .file)).font(.caption).foregroundStyle(.secondary) } }
                        }.disabled(data.busy).swipeActions {
                            if !file.directory {
                                Button("Удалить", role: .destructive) { deleting = NativeRemoteFileTarget(root: data.currentRoot, folder: data.currentPath, file: file) }.disabled(data.busy || store.busy)
                            }
                        }
                    }
                    if data.filesTruncated { Text("Список ограничен агентом. Часть файлов этой папки не показана.").font(.caption).foregroundStyle(.secondary) }
                }
            }
            Section("Буфер обмена") {
                Button("Прочитать буфер ПК") { execute { try await data.getClipboard(device) } }.disabled(data.busy)
                if !data.clipboard.isEmpty {
                    Text(data.clipboard).textSelection(.enabled)
                    Button("Скопировать на iPhone") { UIPasteboard.general.string = data.clipboard; data.status = "Текст скопирован" }
                }
                TextEditor(text: $clipboardDraft).frame(minHeight: 80).accessibilityLabel("Текст для отправки на ПК")
                Button("Отправить текст в буфер ПК") { confirmClipboard = true }.disabled(data.busy || clipboardDraft.isEmpty || clipboardDraft.unicodeScalars.count > 64 * 1024)
            }
            Section("История команд") {
                Button("Обновить историю") { execute { try await data.loadCommands(device) } }.disabled(data.busy)
                ForEach(Array(data.commands.enumerated()), id: \.offset) { _, command in
                    VStack(alignment: .leading, spacing: 5) {
                        Text("\(command["command"] as? String ?? "Команда") · №\(command["id"] as? Int ?? 0)").font(.subheadline.bold())
                        Text(commandState(command["status"] as? String ?? "")).font(.caption).foregroundStyle(.secondary)
                        if let result = command["result"] as? [String: Any], let message = result["message"] as? String { Text(message).font(.caption) }
                        Text(NativeAdvancedStore.date(command["created_at"] as? String ?? "")).font(.caption2).foregroundStyle(.secondary)
                        if command["can_cancel"] as? Bool == true, let id = command["id"] as? Int {
                            Button("Отменить команду") { execute { try await data.cancelCommand(device, id: id) } }.font(.caption).disabled(data.busy)
                        }
                    }
                }
            }
            Section { NavigationLink { NativeWorkspaceKeyImport(origin: store.api.origin) } label: { Label("Импорт ключа защищённых данных", systemImage: "key.horizontal") } }
        }.navigationTitle(device.name).navigationBarTitleDisplayMode(.inline)
            .task { await data.perform { try await data.loadCommands(device) } }
            .quickLookPreview($data.previewURL)
            .onDisappear { commandTask?.cancel(); commandTask = nil; data.clearPreviews() }
            .confirmationDialog("Заменить буфер обмена на ПК?", isPresented: $confirmClipboard, titleVisibility: .visible) {
                Button("Отправить на \(device.name)") { let text = clipboardDraft; execute { try await data.setClipboard(device, text: text) } }
            }
            .confirmationDialog("Удалить файл с ПК?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                if let deleting { Button("Удалить «\(deleting.name)»", role: .destructive) { execute { try await data.deleteFile(device, target: deleting) }; self.deleting = nil } }
            } message: { if let deleting { Text(device.name + " · " + deleting.root + "/" + deleting.path) } }
            .fileImporter(isPresented: $selectingUpload, allowedContentTypes: [.data]) { result in
                switch result {
                case .success(let url):
                    let root = uploadRoot, path = uploadPath
                    execute { try await data.uploadFile(device, url: url, root: root, path: path) }
                case .failure(let error): data.error = error.localizedDescription
                }
            }
    }
    private func execute(_ action: @escaping () async throws -> Void) { commandTask = Task { await data.perform(action) } }
    private func commandState(_ value: String) -> String {
        ["pending": "В очереди", "delivered": "Доставлена агенту", "completed": "Выполнена", "failed": "Ошибка", "cancelled": "Отменена", "expired": "Истёк срок ожидания"][value] ?? value
    }
}

@MainActor private struct NativeWorkspaceKeyImport: View {
    let origin: ServerOrigin
    @State private var selecting = false
    @State private var keyFile: Data?
    @State private var filename = ""
    @State private var password = ""
    @State private var busy = false
    @State private var notice: String?
    @State private var failed = false
    @State private var hasKey = false
    var body: some View {
        Form {
            Section {
                Label(hasKey ? "Ключ сохранён на этом iPhone" : "Ключ ещё не импортирован", systemImage: hasKey ? "checkmark.shield" : "key.horizontal")
                Text("Выберите защищённый файл ключа XASS, экспортированный на устройстве, где привязывали ПК. Он нужен для снимков экрана, файлов и буфера обмена. Пароль и ключ остаются на iPhone.").font(.subheadline).foregroundStyle(.secondary)
                Button(filename.isEmpty ? "Выбрать файл ключа" : filename) { selecting = true }.disabled(busy)
                SecureField("Пароль файла ключа", text: $password).textContentType(.password).disabled(busy)
                Button("Импортировать ключ") { importKey() }.disabled(busy || keyFile == nil || password.unicodeScalars.count < 12)
                if busy { ProgressView("Проверка ключа…") }
                if let notice { Text(notice).foregroundStyle(failed ? Color.red : Color.secondary).textSelection(.enabled) }
            }
        }.navigationTitle("Ключ защищённых данных").navigationBarTitleDisplayMode(.inline)
            .onAppear { hasKey = NativeWorkspaceCrypto.hasKey(origin) }
            .fileImporter(isPresented: $selecting, allowedContentTypes: [.json, .data]) { result in
                do {
                    let url = try result.get(); let scoped = url.startAccessingSecurityScopedResource(); defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                    guard let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 0, size <= 16 * 1024 else { throw OwnerAPIError(status: 0, message: "Файл ключа должен быть не больше 16 КБ.") }
                    keyFile = try Data(contentsOf: url); filename = url.lastPathComponent; notice = nil
                } catch { failed = true; notice = error.localizedDescription }
            }
    }
    private func importKey() {
        guard let file = keyFile else { return }; busy = true; notice = nil
        let pass = password
        Task {
            defer { busy = false }
            do {
                let secret = try await Task.detached(priority: .userInitiated) { try NativeWorkspaceCrypto.importData(file, password: pass) }.value
                try SecureStore.save(secret, name: NativeWorkspaceCrypto.keyName(origin))
                password = ""; keyFile = nil; filename = ""; hasKey = true; failed = false; notice = "Ключ импортирован. Повторите запрос снимка, файла или буфера ПК."
            } catch { failed = true; notice = error.localizedDescription }
        }
    }
}
