import SwiftUI

@MainActor struct NativeWorkspaceMessage: View {
    @ObservedObject var workspace: NativeWorkspaceStore
    var body: some View {
        if let text = workspace.error ?? workspace.notice {
            HStack(alignment: .top) {
                Image(systemName: workspace.error == nil ? "checkmark.circle" : "exclamationmark.circle")
                    .foregroundStyle(workspace.error == nil ? Color.green : Color.orange)
                Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button { workspace.error = nil; workspace.notice = nil } label: { Image(systemName: "xmark") }
                    .accessibilityLabel("Скрыть сообщение")
            }.padding(.vertical, 6)
        }
    }
}

@MainActor struct NativeHomeView: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var workspace: NativeWorkspaceStore
    private var metrics: [String: Any] { workspace.dashboard["metrics"] as? [String: Any] ?? [:] }
    private var systems: [String: Any] { workspace.dashboard["system_status"] as? [String: Any] ?? [:] }
    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack(spacing: 16) {
                        Image("XASSBrand").resizable().scaledToFit().frame(width: 62, height: 62)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(greeting).font(.title2.bold())
                            Text(store.api.origin.host).font(.subheadline).foregroundStyle(.secondary)
                        }
                    }.padding(.vertical, 12).listRowBackground(Color.clear)
                }
                if !store.authorized { NativeLoginPrompt(store: store) }
                NativeWorkspaceMessage(workspace: workspace)
                if store.authorized {
                    Section("Система") {
                        health("Сервер", key: "backend", icon: "server.rack")
                        health("База данных", key: "database", icon: "externaldrive")
                        health("Telegram", key: "telegram_bot", icon: "paperplane")
                        health("Публичный сайт", key: "public_site", icon: "globe")
                    }
                    if !metrics.isEmpty {
                        Section("Нагрузка сервера") {
                            metric("Процессор", key: "cpu_percent")
                            metric("Память", key: "ram_used_percent")
                            metric("Диск", key: "disk_used_percent")
                        }
                    }
                    Section {
                        NavigationLink { NativeNotificationsView(workspace: workspace) } label: {
                            HStack { Label("Уведомления", systemImage: "bell"); Spacer(); Text(String(workspace.unreadCount)).foregroundStyle(.secondary) }
                        }.accessibilityIdentifier("nativeHomeNotifications")
                        NavigationLink { NativeDevicesView(store: store) } label: {
                            HStack { Label("Устройства", systemImage: "desktopcomputer"); Spacer(); Text("\(store.devices.filter(\.online).count) в сети").foregroundStyle(.secondary) }
                        }.accessibilityIdentifier("nativeHomeDevices")
                    }
                    if let track = store.currentTrack {
                        Section("Сейчас играет") {
                            Button { store.showPlayer = true } label: {
                                HStack(spacing: 12) {
                                    TrackArtwork(store: store, trackID: track.id).frame(width: 52, height: 52)
                                    VStack(alignment: .leading, spacing: 4) { Text(track.title).foregroundStyle(.primary); Text(store.deviceLabel).font(.caption).foregroundStyle(.secondary) }
                                    Spacer(); Image(systemName: "waveform").foregroundStyle(XASSStyle.accent)
                                }
                            }
                        }
                    }
                    if let date = workspace.refreshedAt {
                        Text("Обновлено \(date.formatted(date: .omitted, time: .shortened))")
                            .font(.caption).foregroundStyle(.secondary).listRowBackground(Color.clear)
                    }
                }
            }.navigationTitle("Главная").scrollContentBackground(.hidden).background(XASSStyle.background)
                .refreshable { await workspace.load("home"); await store.refresh() }
                .task(id: store.authorized) { if store.authorized { await workspace.load("home") } }
        }
    }
    private var greeting: String {
        let name = workspace.status["name"] as? String ?? ""
        return name.isEmpty ? "Ваш XASS" : name
    }
    private func health(_ name: String, key: String, icon: String) -> some View {
        let data = systems[key] as? [String: Any] ?? [:]
        let available = data["available"] as? Bool
        return HStack {
            Label(name, systemImage: icon); Spacer()
            Text(available == true ? "Работает" : available == false ? "Требует внимания" : "Проверяем…")
                .font(.caption).foregroundStyle(available == true ? Color.green : available == false ? Color.orange : Color.secondary)
        }
    }
    private func metric(_ title: String, key: String) -> some View {
        let value = min(100, NativeValue.number(metrics[key]))
        return VStack(spacing: 8) {
            HStack { Text(title); Spacer(); Text("\(Int(value))%").monospacedDigit().foregroundStyle(.secondary) }
            ProgressView(value: value, total: 100).tint(value >= 90 ? Color.orange : XASSStyle.accent)
        }.padding(.vertical, 4)
    }
}

@MainActor struct NativeToolsView: View {
    @ObservedObject var app: AppState
    @ObservedObject var store: NativeStore
    @ObservedObject var workspace: NativeWorkspaceStore
    var body: some View {
        NavigationStack {
            List {
                if !store.authorized { NativeLoginPrompt(store: store) }
                Section("Мои устройства") {
                    NavigationLink { NativeDevicesView(store: store) } label: { Label("Устройства и управление", systemImage: "desktopcomputer") }.accessibilityIdentifier("nativeToolsDevices")
                    NavigationLink { NativeDownloadsView(store: store, audio: store.audio) } label: { Label("Загрузки и музыка офлайн", systemImage: "arrow.down.circle") }.accessibilityIdentifier("nativeToolsDownloads")
                    NavigationLink { NativeStorageView(store: store) } label: { Label("Музыкальное хранилище", systemImage: "externaldrive") }
                }
                if store.authorized {
                    Section("Управление XASS") {
                        NavigationLink { NativeNotificationsView(workspace: workspace) } label: { Label("Уведомления", systemImage: "bell") }.accessibilityIdentifier("nativeToolsNotifications")
                        NavigationLink { NativeDiagnosticsView(workspace: workspace) } label: { Label("Диагностика сервера", systemImage: "stethoscope") }.accessibilityIdentifier("nativeToolsDiagnostics")
                        NavigationLink { NativeServerPreferencesView(workspace: workspace) } label: { Label("Режимы и уведомления бота", systemImage: "slider.horizontal.3") }
                        NavigationLink { NativeWorkspaceMenu(store: store) } label: { Label("Автоматизация и рабочие инструменты", systemImage: "square.stack.3d.up") }.accessibilityIdentifier("nativeToolsWorkspace")
                    }
                }
                Section {
                    NavigationLink { NativeSettingsView(app: app, store: store) } label: { Label("Настройки приложения", systemImage: "gearshape") }.accessibilityIdentifier("nativeToolsSettings")
                }
            }.navigationTitle("Инструменты").scrollContentBackground(.hidden).background(XASSStyle.background)
        }
    }
}

@MainActor struct NativeNotificationsView: View {
    @ObservedObject var workspace: NativeWorkspaceStore
    var body: some View {
        List {
            NativeWorkspaceMessage(workspace: workspace)
            if workspace.notifications.isEmpty && !workspace.loading {
                ContentUnavailableView("Уведомлений пока нет", systemImage: "bell.slash", description: Text("Здесь появятся события сервера и устройств."))
            }
            ForEach(workspace.notifications) { item in
                VStack(alignment: .leading, spacing: 7) {
                    HStack { if item.unread { Circle().fill(XASSStyle.accent).frame(width: 7, height: 7) }; Text(item.title).font(.headline) }
                    Text(item.message).font(.subheadline).foregroundStyle(.secondary)
                    if let date = ISO8601DateFormatter().date(from: item.createdAt) { Text(date.formatted(date: .abbreviated, time: .shortened)).font(.caption).foregroundStyle(.secondary) }
                }.padding(.vertical, 4)
                    .swipeActions { if item.unread { Button("Прочитано") { Task { await workspace.read(item) } }.tint(XASSStyle.accent) } }
            }
        }.navigationTitle("Уведомления")
            .toolbar { Button("Прочитать всё") { Task { await workspace.readAll() } }.disabled(workspace.saving || workspace.unreadCount == 0) }
            .task { await workspace.load("notifications") }.refreshable { await workspace.load("notifications") }
            .overlay { if workspace.loading && workspace.notifications.isEmpty { ProgressView() } }
    }
}

@MainActor struct NativeDiagnosticsView: View {
    @ObservedObject var workspace: NativeWorkspaceStore
    var body: some View {
        List {
            NativeWorkspaceMessage(workspace: workspace)
            Section("Состояние") {
                LabeledContent("Версия сервера", value: workspace.diagnostics["app_version"] as? String ?? "—")
                state("База данных", section: "database", key: "available")
                state("Telegram настроен", section: "telegram", key: "configured")
                state("Конфигурация", section: "configuration", key: "ok")
                if let storage = workspace.diagnostics["storage"] as? [String: Any], let free = storage["disk_free_gb"] as? NSNumber {
                    LabeledContent("Свободно на сервере", value: String(format: "%.1f ГБ", free.doubleValue))
                }
            }
            let configuration = workspace.diagnostics["configuration"] as? [String: Any] ?? [:]
            let issues = configuration["issues"] as? [String] ?? []
            if !issues.isEmpty { Section("Требует внимания") { ForEach(issues, id: \.self) { Label($0, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) } } }
            Section {
                Button("Проверить ещё раз") { Task { await workspace.load("diagnostics") } }.disabled(workspace.loading)
            }
        }.navigationTitle("Диагностика").task { await workspace.load("diagnostics") }.refreshable { await workspace.load("diagnostics") }
    }
    private func state(_ title: String, section: String, key: String) -> some View {
        let value = (workspace.diagnostics[section] as? [String: Any])?[key] as? Bool
        return LabeledContent(title, value: value == true ? "Да" : value == false ? "Нет" : "—")
    }
}

@MainActor struct NativeServerPreferencesView: View {
    @ObservedObject var workspace: NativeWorkspaceStore
    @State private var message = ""
    var body: some View {
        Form {
            NativeWorkspaceMessage(workspace: workspace)
            Section("Тихие часы") {
                Toggle("Не беспокоить по расписанию", isOn: Binding(get: { workspace.settings["quiet_enabled"] as? Bool ?? false }, set: { _ in Task { await workspace.setting("quiet_toggle", value: true) } })).disabled(workspace.saving || workspace.dashboard.isEmpty)
                Text("Интервал: \(time(workspace.settings["quiet_start"])) – \(time(workspace.settings["quiet_end"]))").font(.caption).foregroundStyle(.secondary)
            }
            Section("Режим отсутствия") {
                Toggle("Я отсутствую", isOn: Binding(get: { workspace.settings["away_enabled"] as? Bool ?? false }, set: { _ in Task { await workspace.setting("away_toggle", value: true) } })).disabled(workspace.saving || workspace.dashboard.isEmpty)
                TextField("Сообщение автоответчика", text: $message, axis: .vertical).lineLimit(3...6)
                Button("Сохранить сообщение") { Task { await workspace.setting("away_message", value: message) } }.disabled(workspace.saving || workspace.dashboard.isEmpty)
            }
        }.navigationTitle("Режимы бота")
            .task { await workspace.load("home"); message = workspace.settings["away_message"] as? String ?? "" }
    }
    private func time(_ value: Any?) -> String {
        guard let minute = value as? Int else { return "—" }
        return String(format: "%02d:%02d", minute / 60, minute % 60)
    }
}

@MainActor struct NativeWeatherView: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var workspace: NativeWorkspaceStore
    private var weather: [String: Any] { workspace.weather["weather"] as? [String: Any] ?? [:] }
    var body: some View {
        NavigationStack {
            List {
                if !store.authorized { NativeLoginPrompt(store: store) }
                NativeWorkspaceMessage(workspace: workspace)
                if !weather.isEmpty {
                    Section {
                        VStack(alignment: .leading, spacing: 14) {
                            Image(systemName: "cloud.sun.fill").font(.system(size: 54)).symbolRenderingMode(.multicolor)
                            Text(text("location_name")).font(.title2.bold())
                            Text(text("temperature")).font(.system(size: 64, weight: .light, design: .rounded))
                            Text(text("weather_text")).font(.title3).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 20).listRowBackground(Color.clear)
                    }
                    Section("Сейчас") {
                        LabeledContent("Ощущается как", value: text("feels_like"))
                        LabeledContent("Ветер", value: text("wind_speed"))
                        LabeledContent("Влажность", value: text("humidity"))
                        LabeledContent("Данные на", value: text("updated_time"))
                    }
                } else if !workspace.loading && store.authorized {
                    ContentUnavailableView("Погода пока недоступна", systemImage: "cloud", description: Text(workspace.status["weather"] as? String ?? "Потяните вниз, чтобы повторить загрузку."))
                }
            }.navigationTitle("Погода").scrollContentBackground(.hidden).background(XASSStyle.background)
                .task(id: store.authorized) { if store.authorized { await workspace.load("weather") } }
                .refreshable { if store.authorized { await workspace.load("weather") } }
                .overlay { if workspace.loading && weather.isEmpty { ProgressView() } }
        }
    }
    private func text(_ key: String) -> String { weather[key] as? String ?? "—" }
}

@MainActor struct NativeSiteView: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var workspace: NativeWorkspaceStore
    @State private var editProfile = false
    @State private var editAppearance = false
    @State private var editProject: NativeSiteProject?
    @State private var deleteProject: NativeSiteProject?
    var body: some View {
        NavigationStack {
            List {
                if !store.authorized { NativeLoginPrompt(store: store) }
                NativeWorkspaceMessage(workspace: workspace)
                if !workspace.site.isEmpty {
                    Section("Профиль") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(workspace.profile.name.isEmpty ? "Ваш профиль" : workspace.profile.name).font(.title2.bold())
                            if !workspace.profile.title.isEmpty { Text(workspace.profile.title).foregroundStyle(.secondary) }
                            if !workspace.profile.bio.isEmpty { Text(workspace.profile.bio).font(.callout) }
                        }.padding(.vertical, 8)
                        if workspace.canEdit {
                            Button { editProfile = true } label: { Label("Изменить профиль", systemImage: "person.crop.circle") }.accessibilityIdentifier("nativeEditSiteProfile")
                            Button { editAppearance = true } label: { Label("Блоки и цвет сайта", systemImage: "paintpalette") }
                        }
                    }
                    Section("Проекты") {
                        ForEach(workspace.projects) { project in
                            Button { if workspace.canEdit { editProject = project } } label: {
                                VStack(alignment: .leading, spacing: 5) {
                                    HStack { Text(project.title).font(.headline).foregroundStyle(.primary); if project.featured { Image(systemName: "star.fill").foregroundStyle(XASSStyle.accent) } }
                                    if !project.subtitle.isEmpty { Text(project.subtitle).font(.subheadline).foregroundStyle(.secondary) }
                                    if !project.description.isEmpty { Text(project.description).font(.callout).foregroundStyle(.secondary).lineLimit(3) }
                                }.padding(.vertical, 5)
                            }.swipeActions { if workspace.canEdit { Button("Удалить", role: .destructive) { deleteProject = project } } }
                        }
                        if workspace.projects.isEmpty { Text("Добавьте первый проект.").foregroundStyle(.secondary) }
                        if workspace.canEdit { Button { editProject = NativeSiteProject() } label: { Label("Добавить проект", systemImage: "plus") } }
                    }
                }
            }.navigationTitle("Сайт").scrollContentBackground(.hidden).background(XASSStyle.background)
                .task(id: store.authorized) { if store.authorized { await workspace.load("site") } }
                .refreshable { if store.authorized { await workspace.load("site") } }
                .sheet(isPresented: $editProfile) { NativeSiteProfileEditor(workspace: workspace, initial: workspace.profile) }
                .sheet(isPresented: $editAppearance) { NativeSiteAppearanceEditor(workspace: workspace) }
                .sheet(item: $editProject) { project in NativeSiteProjectEditor(workspace: workspace, initial: project) }
                .confirmationDialog("Удалить проект с сайта?", isPresented: Binding(get: { deleteProject != nil }, set: { if !$0 { deleteProject = nil } }), titleVisibility: .visible) {
                    Button("Удалить", role: .destructive) { if let project = deleteProject { Task { await workspace.deleteProject(project) } }; deleteProject = nil }
                }
        }
    }
}

@MainActor struct NativeSiteProfileEditor: View {
    @ObservedObject var workspace: NativeWorkspaceStore
    @State private var profile: NativeSiteProfile
    @Environment(\.dismiss) private var dismiss
    init(workspace: NativeWorkspaceStore, initial: NativeSiteProfile) { self.workspace = workspace; _profile = State(initialValue: initial) }
    var body: some View {
        NavigationStack {
            Form {
                NativeWorkspaceMessage(workspace: workspace)
                Section("О себе") {
                    TextField("Имя", text: $profile.name)
                    TextField("Заголовок", text: $profile.title)
                    TextField("Описание", text: $profile.bio, axis: .vertical).lineLimit(4...8)
                    TextField("Цитата", text: $profile.quote, axis: .vertical)
                    TextField("Навыки через запятую", text: $profile.stack)
                }
                Section("Контакты") {
                    TextField("Имя пользователя", text: $profile.username).textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("Ссылка Telegram", text: $profile.telegramURL).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("Ссылка на аватар", text: $profile.avatarURL).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                Section("Ссылки") {
                    ForEach($profile.links) { $link in
                        VStack { TextField("Название", text: $link.label); TextField("https://…", text: $link.url).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled() }
                    }.onDelete { profile.links.remove(atOffsets: $0) }
                    Button("Добавить ссылку") { profile.links.append(NativeSiteLink()) }.disabled(profile.links.count >= 16)
                }
            }.navigationTitle("Профиль сайта").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() }.disabled(workspace.saving) }
                    ToolbarItem(placement: .confirmationAction) { Button("Сохранить") { Task { if await workspace.saveProfile(profile) { dismiss() } } }.disabled(workspace.saving) }
                }
        }.interactiveDismissDisabled(workspace.saving)
    }
}

@MainActor struct NativeSiteProjectEditor: View {
    @ObservedObject var workspace: NativeWorkspaceStore
    @State private var project: NativeSiteProject
    @Environment(\.dismiss) private var dismiss
    init(workspace: NativeWorkspaceStore, initial: NativeSiteProject) { self.workspace = workspace; _project = State(initialValue: initial) }
    var body: some View {
        NavigationStack {
            Form {
                NativeWorkspaceMessage(workspace: workspace)
                Section("Проект") {
                    TextField("Название", text: $project.title)
                    TextField("Подзаголовок", text: $project.subtitle)
                    TextField("Описание", text: $project.description, axis: .vertical).lineLimit(4...8)
                    TextField("Ссылка", text: $project.url).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("Теги через запятую", text: $project.tags)
                    Toggle("Главный проект", isOn: $project.featured)
                }
                Section("Состояние") {
                    Picker("Статус", selection: $project.status) {
                        Text("В разработке").tag("dev"); Text("Тестируется").tag("testing"); Text("Работает").tag("working")
                        Text("Стабильный").tag("stable"); Text("Нестабильный").tag("unstable"); Text("Архив").tag("archived")
                    }
                    Stepper("Начало: \(project.yearFrom)", value: $project.yearFrom, in: 1970...2100)
                    Stepper("Окончание: \(project.yearTo)", value: $project.yearTo, in: 1970...2100)
                }
                Section("Обложка") {
                    Picker("Тип", selection: $project.coverType) { Text("Изображение").tag("image"); Text("Видео").tag("video") }
                    TextField("Ссылка на обложку", text: $project.coverSource).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                }
            }.navigationTitle(project.id.isEmpty ? "Новый проект" : "Изменить проект").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() }.disabled(workspace.saving) }
                    ToolbarItem(placement: .confirmationAction) { Button("Сохранить") { Task { if await workspace.saveProject(project) { dismiss() } } }.disabled(workspace.saving || project.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || project.yearTo < project.yearFrom) }
                }
        }.interactiveDismissDisabled(workspace.saving)
    }
}

@MainActor struct NativeSiteAppearanceEditor: View {
    @ObservedObject var workspace: NativeWorkspaceStore
    @State private var accent = "#376dff"
    @State private var widgets: [String] = []
    @Environment(\.dismiss) private var dismiss
    private let options = [("about", "О себе"), ("quotes", "Цитаты"), ("music", "Музыка"), ("weather", "Погода"), ("projects", "Проекты"), ("contacts", "Контакты")]
    var body: some View {
        NavigationStack {
            Form {
                NativeWorkspaceMessage(workspace: workspace)
                Section("Акцентный цвет") { TextField("#376dff", text: $accent).textInputAutocapitalization(.never).autocorrectionDisabled() }
                Section("Блоки сайта") {
                    ForEach(options, id: \.0) { key, label in
                        Toggle(label, isOn: Binding(get: { widgets.contains(key) }, set: { enabled in if enabled { if !widgets.contains(key) { widgets.append(key) } } else { widgets.removeAll { $0 == key } } }))
                    }
                }
            }.navigationTitle("Оформление").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() }.disabled(workspace.saving) }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Сохранить") { Task { if await workspace.save("/api/mini/site/config", body: ["accent_color": accent, "widgets": widgets]) != nil { await workspace.load("site"); dismiss() } } }
                            .disabled(workspace.saving || accent.range(of: "^#[0-9a-fA-F]{6}$", options: .regularExpression) == nil)
                    }
                }
                .onAppear { let config = workspace.site["site_config"] as? [String: Any] ?? [:]; accent = config["accent_color"] as? String ?? "#376dff"; widgets = config["widgets"] as? [String] ?? [] }
        }.interactiveDismissDisabled(workspace.saving)
    }
}
