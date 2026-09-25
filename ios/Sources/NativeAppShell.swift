import SwiftUI

/// Shipping shell: every destination is a native view, including sign-in and settings.
@MainActor struct NativeAppShell: View {
    @ObservedObject var app: AppState
    @ObservedObject var store: NativeStore
    @State private var selectedTab = 0
    @State private var preview: NativeAppModal?
    @State private var presentation = NativeModalPresentation()

    private var requestedModal: NativeAppModal? {
        if store.showLogin || store.showEnrollment { return .enrollment }
        if store.showRoutePicker { return .routes }
        if store.showPlayer { return .player }
        return preview
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            library
                .modifier(NativePlaybackInset(store: store))
                .tabItem { Label("Музыка", systemImage: "music.note") }.tag(0)
            NativeDevicesView(store: store)
                .modifier(NativePlaybackInset(store: store))
                .tabItem { Label("Устройства", systemImage: "desktopcomputer") }.tag(1)
            NativeDownloadsView(store: store, audio: store.audio)
                .modifier(NativePlaybackInset(store: store))
                .tabItem { Label("Загрузки", systemImage: "arrow.down.to.line") }.tag(2)
            NativeAppSettingsView(app: app, store: store)
                .modifier(NativePlaybackInset(store: store))
                .tabItem { Label("Настройки", systemImage: "gearshape") }.tag(3)
        }
        .tint(XASSStyle.accent)
        .sheet(item: Binding(get: { presentation.current }, set: { value in
            if value == nil { presentation.systemDismissed() }
        }), onDismiss: {
            if presentation.didDismiss() {
                store.showLogin = false
                store.showEnrollment = false
                store.showRoutePicker = false
                store.showPlayer = false
                preview = nil
            }
        }) { destination in
            switch destination {
            case .enrollment: NativeEnrollmentView(store: store)
            case .routes: NativeAppRoutePicker(store: store)
            case .player: NativePlayerView(store: store)
            case .devicePreview:
                NavigationStack {
                    if let device = store.devices.first {
                        NativeDeviceDetail(store: store, deviceID: device.id)
                    }
                }
            case .storagePreview:
                NavigationStack { NativeStorageView(store: store) }
            }
        }
        .onChange(of: requestedModal) { _, destination in presentation.request(destination) }
        .onChange(of: app.locked) { _, locked in
            if locked && !app.nativeConfirmation {
                store.showLogin = false
                store.showEnrollment = false
            }
        }
        .onAppear {
            #if DEBUG && targetEnvironment(simulator)
            if NativeFixture.enabled {
                switch NativeFixture.screen {
                case "player": store.showPlayer = true
                case "devices": selectedTab = 1; preview = .devicePreview
                case "routes": store.showRoutePicker = true
                case "storage": selectedTab = 3; preview = .storagePreview
                default: break
                }
            }
            #endif
            presentation.request(requestedModal)
        }
    }

    @ViewBuilder private var library: some View {
        if store.authorized {
            NativeLibraryView(store: store)
        } else {
            NavigationStack {
                List {
                    Section {
                        Label("Подключите свой аккаунт", systemImage: "person.crop.circle.badge.checkmark")
                            .font(.headline)
                        Text("Вставьте одноразовую ссылку XASS. Музыка и управление устройствами останутся внутри приложения.")
                        Button("Вставить ссылку и войти") { store.showEnrollment = true }
                            .buttonStyle(.borderedProminent)
                            .accessibilityIdentifier("nativeSignIn")
                    }
                    Section {
                        Text("Уже скачанная музыка доступна на вкладке «Загрузки», даже без подключения к серверу.")
                            .foregroundStyle(.secondary)
                    }
                    NativeMessage(store: store)
                }
                .navigationTitle("Музыка")
                .refreshable { await store.refresh() }
            }
        }
    }
}

/// Attach to each tab's content, not the TabView: the player stays above the tab bar.
@MainActor private struct NativePlaybackInset: ViewModifier {
    @ObservedObject var store: NativeStore
    func body(content: Content) -> some View {
        content.safeAreaInset(edge: .bottom, spacing: 0) {
            if store.transferStatus != nil && !store.showRoutePicker {
                NativeTransferWait(store: store)
            } else if store.transferStatus == nil {
                NativeMiniPlayer(store: store)
            }
        }
    }
}

@MainActor struct NativeAppSettingsView: View {
    @ObservedObject var app: AppState
    @ObservedObject var store: NativeStore
    @State private var forget = false

    var body: some View {
        NavigationStack {
            List {
                Section("Подключение") {
                    NavigationLink {
                        NativeServerAccessView(store: store)
                    } label: {
                        Label("Сервер и доступ", systemImage: "server.rack")
                    }.accessibilityIdentifier("nativeServerAccess")
                    Button {
                        store.showEnrollment = true
                    } label: {
                        Label(store.enrolled ? "Защищённый ключ iPhone" : "Привязать iPhone", systemImage: "lock.shield")
                    }.accessibilityIdentifier("nativeEnrollmentSettings")
                }
                Section("Музыка") {
                    NavigationLink {
                        NativeStorageView(store: store)
                    } label: {
                        Label("Хранение", systemImage: "externaldrive")
                    }
                    Button { store.openRoutePicker() } label: {
                        Label("Устройство воспроизведения", systemImage: "airplayaudio")
                    }.accessibilityIdentifier("nativeSettingsRoutes")
                    Button("Очистить кэш обложек") { store.clearArtworkCache() }
                }
                Section("Защита приложения") {
                    Label("Face ID или код-пароль", systemImage: "faceid")
                    Text("При возвращении приложение защищает доступ к управлению. Музыка может продолжать играть в фоне.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section {
                    Button("Выйти и сменить сервер", role: .destructive) { forget = true }
                    Text("XASS \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "")")
                        .foregroundStyle(.secondary)
                }
                NativeMessage(store: store)
            }
            .navigationTitle("Настройки")
            .confirmationDialog("Выйти из XASS на этом iPhone?", isPresented: $forget, titleVisibility: .visible) {
                Button("Выйти и сменить сервер", role: .destructive) { app.forgetServer() }
                Button("Отмена", role: .cancel) {}
            } message: {
                Text("Данные сервера и сохранённые треки не удаляются.")
            }
        }
    }
}

@MainActor struct NativeServerAccessView: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        List {
            Section("Ваш сервер") {
                Text(store.api.origin.url.absoluteString).textSelection(.enabled)
                LabeledContent("Сессия", value: store.authorized ? "Вход выполнен" : "Требуется вход")
                LabeledContent("Ключ iPhone", value: store.enrolled ? "Привязан" : "Не привязан")
                Button {
                    store.run { await store.refresh() }
                } label: {
                    HStack {
                        Text("Обновить данные")
                        Spacer()
                        if store.loading { ProgressView() }
                    }
                }.disabled(store.loading)
            }
            Section("Устройства") {
                LabeledContent("Подключено к аккаунту", value: String(store.devices.count))
                LabeledContent("Сейчас в сети", value: String(store.devices.filter { $0.online }.count))
            }
            Section {
                Toggle("Показывать текущий трек на сайте", isOn: Binding(get: { store.shareSite }, set: { desired in
                    store.run { try await store.setSharing(desired) }
                }))
                .disabled(!store.authorized || store.currentID == nil || store.shareSaving || store.busy)
                if store.shareSaving { ProgressView("Сохраняю…") }
            } header: {
                Text("Публикация музыки")
            } footer: {
                Text("Настройка меняется через API без открытия сайта. Чтобы её изменить, выберите трек.")
            }
            NativeMessage(store: store)
        }
        .navigationTitle("Сервер и доступ")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await store.refresh() }
    }
}
