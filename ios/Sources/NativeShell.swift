import SwiftUI
import AVKit

private enum NativeMainSheet: String, Identifiable {
    case player, route, enrollment
    var id: String { rawValue }
}

@MainActor struct NativeShell: View {
    @ObservedObject var app: AppState
    @ObservedObject var store: NativeStore
    @StateObject private var workspace: NativeWorkspaceStore
    @State private var selectedTab = 0
    @State private var fixtureDevice = false
    @State private var fixtureStorage = false
    init(app: AppState, store: NativeStore) {
        self.app = app; self.store = store
        _workspace = StateObject(wrappedValue: NativeWorkspaceStore(api: store.api))
    }
    var body: some View {
        TabView(selection: $selectedTab) {
            content(NativeHomeView(store: store, workspace: workspace)).tabItem { Label("Главная", systemImage: "house") }.tag(0).accessibilityIdentifier("tab-home")
            content(NativeLibraryView(store: store)).tabItem { Label("Музыка", systemImage: "music.note") }.tag(1).accessibilityIdentifier("tab-music")
            content(NativeSiteView(store: store, workspace: workspace)).tabItem { Label("Сайт", systemImage: "person.crop.square") }.tag(2).accessibilityIdentifier("tab-site")
            content(NativeToolsView(app: app, store: store, workspace: workspace)).tabItem { Label("Инструменты", systemImage: "square.grid.2x2") }.tag(3).accessibilityIdentifier("tab-tools")
            content(NativeWeatherView(store: store, workspace: workspace)).tabItem { Label("Погода", systemImage: "cloud.sun") }.tag(4).accessibilityIdentifier("tab-weather")
        }.tint(XASSStyle.accent)
            .sheet(item: presentedSheet) { sheet in
                switch sheet {
                case .player: NativePlayerView(store: store)
                case .route: NativeRoutePicker(store: store)
                case .enrollment: NativeEnrollmentView(store: store)
                }
            }
            .onChange(of: store.showRoutePicker) { _, open in
                if open { store.showPlayer = false; store.showLogin = false; store.showEnrollment = false }
            }
            .onChange(of: store.showPlayer) { _, open in
                if open { store.showRoutePicker = false }
            }
            .onChange(of: store.showLogin) { _, open in
                if open { store.showRoutePicker = false; store.showPlayer = false; store.showLogin = false; store.showEnrollment = true }
            }
            .sheet(isPresented: $fixtureDevice) { NavigationStack { if let device = store.devices.first { NativeDeviceDetail(store: store, deviceID: device.id) } } }
            .sheet(isPresented: $fixtureStorage) { NavigationStack { NativeStorageView(store: store) } }
            .onAppear {
                #if DEBUG && targetEnvironment(simulator)
                if NativeFixture.enabled {
                    switch NativeFixture.screen {
                    case "library": selectedTab = 1
                    case "player": selectedTab = 1; store.showPlayer = true
                    case "devices": selectedTab = 3; fixtureDevice = true
                    case "routes": selectedTab = 1; store.showRoutePicker = true
                    case "storage": selectedTab = 3; fixtureStorage = true
                    case "site": selectedTab = 2
                    case "tools": selectedTab = 3
                    case "weather": selectedTab = 4
                    default: break
                    }
                }
                #endif
            }
            .onChange(of: app.locked) { _, locked in
                // Keep Now Playing / player sheet across Control Center and lock:
                // scenePhase .inactive sets app.locked, but must not dismiss playback UI.
                if locked { store.showLogin = false; if !app.nativeConfirmation { store.showEnrollment = false } }
            }
    }
    private var presentedSheet: Binding<NativeMainSheet?> {
        Binding(get: {
            if store.showRoutePicker { return .route }
            if store.showPlayer { return .player }
            if store.showEnrollment { return .enrollment }
            return nil
        }, set: { value in
            if value == nil { store.showRoutePicker = false; store.showPlayer = false; store.showEnrollment = false }
        })
    }
    private func content<Content: View>(_ view: Content) -> some View {
        // Reserve space inside each tab's content area. An inset on TabView itself
        // overlaps the native tab bar on iPhone and intercepts navigation taps.
        view.safeAreaInset(edge: .bottom, spacing: 0) {
            if store.transferStatus != nil && !store.showRoutePicker { NativeTransferWait(store: store) }
            else if store.transferStatus == nil { NativeMiniPlayer(store: store) }
        }
    }
}

@MainActor struct NativeLoginPrompt: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Войдите на свой сервер", systemImage: "person.crop.circle.badge.checkmark").font(.headline)
            Text("Создайте одноразовую ссылку в боте XASS в Telegram и вставьте её здесь. Она откроет библиотеку и привяжет этот iPhone.").font(.callout).foregroundStyle(.secondary)
            Button("Вставить одноразовую ссылку") { store.showEnrollment = true }.buttonStyle(.borderedProminent)
        }.padding(.vertical, 14).accessibilityIdentifier("nativeLoginPrompt")
    }
}

@MainActor struct NativeEnrollmentView: View {
    @ObservedObject var store: NativeStore
    @State private var pair = ""
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label("Защищённый ключ этого iPhone", systemImage: "lock.shield").font(.headline)
                    Text("Откройте XASS в Telegram → Инструменты → iPhone и веб-приложение → создайте новую защищённую ссылку. Вставьте её ниже.")
                    Text("Ссылка одноразовая. Face ID или код-пароль подтверждает действия, а закрытый ключ остаётся в Secure Enclave iPhone.").font(.footnote).foregroundStyle(.secondary)
                }
                Section("Ссылка или ключ") {
                    SecureField("xpw_… или https://…#pair=…", text: $pair).textInputAutocapitalization(.never).autocorrectionDisabled().accessibilityIdentifier("nativeEnrollmentPair")
                    Text(store.api.origin.url.absoluteString).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                NativeMessage(store: store)
                Button {
                    let input = pair; pair = ""
                    store.run { try await store.enroll(pairInput: input); dismiss() }
                } label: { HStack { Text(store.busy ? "Подтверждаю…" : "Привязать iPhone"); Spacer(); if store.busy { ProgressView() } else { Image(systemName: "faceid") } } }
                    .disabled(pair.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || store.busy).accessibilityIdentifier("nativeEnrollConfirm")
            }.navigationTitle("Привязка iPhone").navigationBarTitleDisplayMode(.inline)
                .toolbar { Button("Отмена") { pair = ""; dismiss() }.disabled(store.busy) }
        }.interactiveDismissDisabled(store.busy)
    }
}

@MainActor struct NativeDevicesView: View {
    @ObservedObject var store: NativeStore
    var body: some View {
            List {
                if !store.authorized { NativeLoginPrompt(store: store) }
                ForEach(store.devices) { device in
                    NavigationLink { NativeDeviceDetail(store: store, deviceID: device.id) } label: {
                        HStack(spacing: 14) {
                            Image(systemName: "desktopcomputer").font(.title2).frame(width: 40)
                            VStack(alignment: .leading, spacing: 4) { Text(device.name).font(.headline).accessibilityLabel(device.name); HStack(spacing: 5) { Circle().fill(device.online ? Color.green : Color.gray).frame(width: 6, height: 6); Text(device.online ? "В сети" : "Не в сети").font(.caption).foregroundStyle(.secondary) } }
                        }.padding(.vertical, 6)
                    }.accessibilityElement(children: .combine)
                    .accessibilityAddTraits(.isButton)
                    .accessibilityLabel(device.name)
                    .accessibilityIdentifier("native-device-\(device.id)")
                }
                if store.authorized && store.devices.isEmpty { ContentUnavailableView("Нет подключённых ПК", systemImage: "desktopcomputer", description: Text("Добавьте Windows-агент в Telegram Mini App и импортируйте его конфигурацию на ПК.")) }
                NativeMessage(store: store)
            }.navigationTitle("Устройства").refreshable { await store.refresh() }
    }
}

@MainActor struct NativeDeviceDetail: View {
    @ObservedObject var store: NativeStore
    let deviceID: Int
    @State private var command: String?
    @State private var detach = false
    @Environment(\.dismiss) private var dismiss
    private var device: NativeDevice? { store.devices.first { $0.id == deviceID } }
    var body: some View {
        List {
            if let device = device {
                HStack(spacing: 6) { Circle().fill(device.online ? Color.green : Color.gray).frame(width: 8, height: 8); Text(device.online ? "В сети" : "Не в сети").foregroundStyle(device.online ? Color.green : Color.secondary); Text("· \(device.version)").foregroundStyle(.secondary) }.font(.subheadline).listRowBackground(Color.clear)
                Section("Управление") {
                    action("Заблокировать экран", icon: "lock", command: "lock", device: device)
                    action("Перезапустить агент", icon: "arrow.clockwise", command: "restart", device: device)
                    action("Обновить агент", icon: "arrow.down.circle", command: "update", device: device)
                    Menu { Button("Режим сна") { command = "sleep" }; Button("Перезагрузить Windows") { command = "reboot" }; Button("Выключить ПК", role: .destructive) { command = "shutdown" } } label: { Label("Питание Windows", systemImage: "power") }.disabled(!device.online || store.busy)
                }
                Section("Музыкальное хранилище") {
                    NavigationLink { NativeStorageView(store: store, source: device.name) } label: { Label("Сохранённые треки", systemImage: "music.note") }
                }
                Section("Рабочая область") {
                    NavigationLink { NativePCWorkspace(store: store, device: device) } label: { Label("Экран, файлы и буфер обмена", systemImage: "rectangle.connected.to.line.below") }
                }
                Section { Button(role: .destructive) { detach = true } label: { Label("Отвязать агент", systemImage: "trash") }.disabled(store.busy).accessibilityIdentifier("nativeDetachAgent") } header: { Text("Опасная зона") } footer: { Text("Файлы и архивы сохранятся.") }
                NativeMessage(store: store)
            } else { ContentUnavailableView("ПК отвязан", systemImage: "desktopcomputer") }
        }.navigationTitle(device?.name ?? "Устройство")
            .confirmationDialog("Подтвердить действие с ПК \(device?.name ?? "")?", isPresented: Binding(get: { command != nil }, set: { if !$0 { command = nil } }), titleVisibility: .visible) {
                Button(commandLabel) { if let device = device, let action = command { store.run { try await store.deviceCommand(device, command: action) } }; command = nil }
                Button("Отмена", role: .cancel) { command = nil }
            } message: { Text("Потребуется Face ID или код-пароль. Блокировка экрана не отключает пароль Windows; разблокируйте ПК обычным способом.") }
            .confirmationDialog("Отвязать \(device?.name ?? "ПК") от XASS?", isPresented: $detach, titleVisibility: .visible) {
                Button("Отвязать агент", role: .destructive) { if let device = device { store.run { try await store.detach(device); dismiss() } } }
                Button("Отмена", role: .cancel) {}
            } message: { Text("Ключ ПК будет отозван, очередь команд отменена. Для повторного подключения нужна новая привязка. Архивы не удаляются.") }
    }
    private var commandLabel: String { ["lock": "Заблокировать экран", "restart": "Перезапустить агент", "update": "Обновить агент", "sleep": "Перевести в сон", "reboot": "Перезагрузить Windows", "shutdown": "Выключить ПК"][command ?? ""] ?? "Подтвердить" }
    private func action(_ title: String, icon: String, command value: String, device: NativeDevice) -> some View {
        Button { command = value } label: { Label(title, systemImage: icon).foregroundStyle(.primary).padding(.vertical, 5) }.disabled(!device.online || store.busy)
    }
}

@MainActor struct NativeSettingsView: View {
    @ObservedObject var app: AppState
    @ObservedObject var store: NativeStore
    @State private var forget = false
    var body: some View {
            List {
                Section("Сервер") { Text(store.api.origin.url.absoluteString).textSelection(.enabled); Button("Обновить данные") { store.run { await store.refresh() } } }
                Section("Приложение") {
                    NavigationLink { NativeStorageView(store: store) } label: { Label("Хранение", systemImage: "externaldrive") }
                    Button { store.showEnrollment = true } label: { Label(store.enrolled ? "Защищённый ключ iPhone привязан" : "Привязать ключ iPhone", systemImage: "lock.shield") }
                    Label("Face ID или код-пароль при возвращении", systemImage: "faceid").font(.callout)
                }
                Section { Button("Выйти и сменить сервер", role: .destructive) { forget = true }; Text("XASS \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "")").foregroundStyle(.secondary) }
                NativeMessage(store: store)
            }.navigationTitle("Настройки")
                .confirmationDialog("Выйти из XASS на этом iPhone?", isPresented: $forget, titleVisibility: .visible) { Button("Выйти и сменить сервер", role: .destructive) { app.forgetServer() } } message: { Text("Данные сервера и сохранённые треки не удаляются.") }
    }
}

@MainActor struct NativeDownloadsView: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var audio: AudioController
    @State private var remove: DownloadedTrack?
    @State private var offline: DownloadedTrack?
    var body: some View {
            List {
                if audio.downloads.isEmpty && audio.cachedTracks.isEmpty { ContentUnavailableView("Музыка без интернета", systemImage: "arrow.down.circle", description: Text("Сохраните треки из плеера. Они доступны в этом приложении без сети.")) }
                if !audio.downloadIDs.isEmpty { ProgressView("Сохраняю треки: \(audio.downloadIDs.count)") }
                ForEach(audio.downloads) { track in
                    HStack(spacing: 12) {
                        Button {
                            if store.authorized { store.run { do { try await store.transfer(to: "local", trackID: track.id, startPosition: 0) } catch { offline = track; throw error } } }
                            else { store.playOffline(track) }
                        } label: { HStack(spacing: 12) { TrackArtwork(store: store, trackID: track.id).frame(width: 44, height: 44); VStack(alignment: .leading, spacing: 3) { Text(track.title).foregroundStyle(.primary); Text(track.artist.isEmpty ? "На этом iPhone" : track.artist).font(.caption).foregroundStyle(.secondary) }; Spacer() } }.buttonStyle(.plain)
                        Button { remove = track } label: { Image(systemName: "ellipsis").frame(width: 40, height: 44) }.buttonStyle(.borderless).accessibilityLabel("Удалить локальную копию")
                    }
                }
                if !audio.cachedTracks.isEmpty {
                    Section("Автоматический кэш") {
                        ForEach(audio.cachedTracks.filter { entry in !audio.downloads.contains(where: { $0.id == entry.id }) }) { entry in
                            Button {
                                if store.authorized { store.run { do { try await store.transfer(to: "local", trackID: entry.id, startPosition: 0) } catch { offline = entry.track; throw error } } }
                                else { store.playOffline(entry.track) }
                            } label: {
                                HStack { VStack(alignment: .leading) { Text(entry.track.title).foregroundStyle(.primary); Text(entry.track.artist).font(.caption).foregroundStyle(.secondary) }; Spacer(); Text(ByteCountFormatter.string(fromByteCount: entry.bytes, countStyle: .file)).font(.caption).foregroundStyle(.secondary) }
                            }
                        }
                    }
                }
                NativeMessage(store: store)
            }.navigationTitle("Загрузки")
                .confirmationDialog("Удалить только копию с iPhone? На сервере трек останется.", isPresented: Binding(get: { remove != nil }, set: { if !$0 { remove = nil } }), titleVisibility: .visible) {
                    Button("Удалить локальную копию", role: .destructive) { if let track = remove { audio.removeDownload(track) }; remove = nil }
                }
                .confirmationDialog("Слушать только на этом iPhone?", isPresented: Binding(get: { offline != nil }, set: { if !$0 { offline = nil } }), titleVisibility: .visible) {
                    Button("Слушать локальную копию") { if let track = offline { store.playOffline(track) }; offline = nil }
                    Button("Отмена", role: .cancel) { offline = nil }
                } message: { Text("Сервер не подтвердил переключение. В офлайн-режиме XASS не может остановить музыку на другом устройстве. Продолжайте только если там ничего не играет.") }
    }
}

struct NativeTransferWait: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 12) {
                ProgressView().tint(.white)
                VStack(alignment: .leading, spacing: 2) {
                    Text(store.transferStatus ?? "Переключаю…").font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                    Text("Можно отменить и остаться на текущем устройстве").font(.caption2).foregroundStyle(.white.opacity(0.7))
                }
                Spacer(minLength: 0)
                Button("Отмена") { store.cancelTransfer() }
                    .font(.subheadline.weight(.semibold))
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(Color.white.opacity(0.15), in: Capsule())
                    .foregroundStyle(.white)
                    .accessibilityIdentifier("nativeTransferCancel")
            }
            ProgressView(value: min(max(store.transferProgress, 0), 1))
                .tint(XASSStyle.accent)
                .accessibilityIdentifier("nativeTransferProgress")
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
        .background(Color.black.opacity(0.92))
        .overlay(alignment: .top) { Divider().overlay(Color.white.opacity(0.12)) }
    }
}

/// AirPlay-like single sheet: pick iPhone/PC and watch handoff progress here (not a nested sheet).
@MainActor struct NativeRoutePicker: View {
    @ObservedObject var store: NativeStore
    @Environment(\.dismiss) private var dismiss
    @State private var selectedPC: NativeDevice?
    @State private var loadingOutputs = false
    @State private var targetOutput = "default"

    var body: some View {
        NavigationStack {
            List {
                if let status = store.transferStatus {
                    Section {
                        VStack(alignment: .leading, spacing: 10) {
                            HStack(spacing: 10) {
                                ProgressView()
                                Text(status).font(.subheadline.weight(.semibold))
                                Spacer(minLength: 0)
                            }
                            ProgressView(value: min(max(store.transferProgress, 0), 1))
                                .tint(XASSStyle.accent)
                                .accessibilityIdentifier("nativeTransferProgress")
                            Button("Отмена") { store.cancelTransfer() }
                                .accessibilityIdentifier("nativeTransferCancel")
                        }
                        .padding(.vertical, 4)
                    } header: { Text("Переключение") }
                } else {
                    Section("Куда играть") {
                        Button {
                            store.run { try await store.pickRoute(device: "local") }
                        } label: {
                            HStack {
                                Label("Этот iPhone", systemImage: "iphone")
                                Spacer()
                                if store.selectedDevice == "local" { Image(systemName: "checkmark") }
                            }
                        }
                        .disabled(store.busy)
                        .accessibilityIdentifier("route-local")

                        ForEach(store.devices) { device in
                            Button {
                                selectedPC = device
                                targetOutput = "default"
                                loadingOutputs = true
                                store.run {
                                    defer { loadingOutputs = false }
                                    try await store.loadOutputs(source: device.name)
                                }
                            } label: {
                                HStack {
                                    Image(systemName: "desktopcomputer")
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(device.name)
                                        Text(device.online ? "В сети" : "Не в сети")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if store.selectedDevice == "agent:" + device.name {
                                        Image(systemName: "checkmark")
                                    }
                                }
                            }
                            .disabled(!device.online || store.busy)
                            .accessibilityIdentifier("route-device-\(device.id)")
                            .accessibilityLabel(Text(device.name))
                        }
                    }

                    if let device = selectedPC {
                        Section {
                            if loadingOutputs { ProgressView("Получаю выходы Windows…") }
                            ForEach(store.outputs) { output in
                                Button { targetOutput = output.id } label: {
                                    HStack {
                                        Label(output.name, systemImage: "speaker.wave.2")
                                        Spacer()
                                        if targetOutput == output.id { Image(systemName: "checkmark") }
                                    }
                                }
                            }
                            Button("Слушать здесь") {
                                store.run {
                                    try await store.pickRoute(device: "agent:" + device.name, output: targetOutput)
                                }
                            }
                            .disabled(store.busy || loadingOutputs || !device.online)
                            .accessibilityIdentifier("route-confirm")
                        } header: {
                            Text("Аудиовыход · \(device.name)")
                        }
                    }
                    Section("AirPlay и Bluetooth") { NativeSystemAudioRoute() }
                }
            }
            .navigationTitle("Куда играть")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Закрыть") {
                        if store.transferStatus != nil { store.cancelTransfer() }
                        store.showRoutePicker = false
                        dismiss()
                    }
                }
            }
            .onAppear {
                selectedPC = store.devices.first(where: { "agent:" + $0.name == store.selectedDevice })
                targetOutput = store.outputID
                if let device = selectedPC {
                    loadingOutputs = true
                    store.run { defer { loadingOutputs = false }; try await store.loadOutputs(source: device.name) }
                }
            }
            .onChange(of: store.showRoutePicker) { _, open in
                if !open { dismiss() }
            }
        }
        .presentationDetents([.medium, .large])
        .interactiveDismissDisabled(store.transferStatus != nil)
    }
}

struct NativeSystemAudioRoute: View {
    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Выход звука iPhone")
                Text("Наушники, колонки и AirPlay").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            NativeAudioRouteButton().frame(width: 48, height: 48).accessibilityLabel("Выбрать AirPlay или наушники")
        }
    }
}

private struct NativeAudioRouteButton: UIViewRepresentable {
    func makeUIView(context: Context) -> AVRoutePickerView {
        let view = AVRoutePickerView()
        view.tintColor = .white; view.activeTintColor = .systemBlue; view.prioritizesVideoDevices = false
        return view
    }
    func updateUIView(_ view: AVRoutePickerView, context: Context) { }
}
