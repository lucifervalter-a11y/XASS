import SwiftUI

@MainActor struct NativeShell: View {
    @ObservedObject var app: AppState
    @ObservedObject var store: NativeStore
    @State private var selectedTab = 0
    @State private var fixtureRoutes = false
    @State private var fixtureDevice = false
    @State private var fixtureStorage = false
    var body: some View {
        TabView(selection: $selectedTab) {
            NativeLibraryView(store: store).tabItem { Label("Музыка", systemImage: "music.note") }.tag(0)
            NativeDevicesView(store: store).tabItem { Label("Устройства", systemImage: "desktopcomputer") }.tag(1)
            NativeDownloadsView(store: store, audio: store.audio).tabItem { Label("Загрузки", systemImage: "arrow.down.to.line") }.tag(2)
            NativeSettingsView(app: app, store: store).tabItem { Label("Настройки", systemImage: "gearshape") }.tag(3)
        }.tint(XASSStyle.accent)
            .sheet(isPresented: $store.showPlayer) { NativePlayerView(store: store) }
            .sheet(isPresented: $store.showLogin) {
                NavigationStack {
                    WebContainer(app: app, origin: store.api.origin).navigationTitle("Вход через Telegram").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Закрыть") { store.showLogin = false } } }
                }
            }
            .sheet(isPresented: $store.showEnrollment) { NativeEnrollmentView(store: store) }
            .sheet(isPresented: $fixtureRoutes) { NativePlayerDevices(store: store) }
            .sheet(isPresented: $fixtureDevice) { NavigationStack { if let device = store.devices.first { NativeDeviceDetail(store: store, deviceID: device.id) } } }
            .sheet(isPresented: $fixtureStorage) { NavigationStack { NativeStorageView(store: store) } }
            .onAppear {
                #if DEBUG && targetEnvironment(simulator)
                if NativeFixture.enabled {
                    switch NativeFixture.screen {
                    case "player": store.showPlayer = true
                    case "devices": selectedTab = 1; fixtureDevice = true
                    case "routes": fixtureRoutes = true
                    case "storage": selectedTab = 3; fixtureStorage = true
                    default: break
                    }
                }
                #endif
            }
            .onChange(of: app.locked) { _, locked in
                if locked { store.showPlayer = false; store.showLogin = false; if !app.nativeConfirmation { store.showEnrollment = false } }
            }
    }
}

@MainActor struct NativeLoginPrompt: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Войдите на свой сервер", systemImage: "person.crop.circle.badge.checkmark").font(.headline)
            Text("Вход через Telegram открывает библиотеку. Одноразовая ссылка также привяжет защищённый ключ iPhone для команд ПК.").font(.callout).foregroundStyle(.secondary)
            Button("Вставить одноразовую ссылку") { store.showEnrollment = true }.buttonStyle(.borderedProminent)
            Button("Войти через Telegram") { store.showLogin = true }
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

@MainActor struct NativePlayerDevices: View {
    @ObservedObject var store: NativeStore
    @State private var selectedPC: NativeDevice?
    @State private var loadingOutputs = false
    @State private var targetDevice = "local"
    @State private var targetOutput = "default"
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List {
                Section("Устройство воспроизведения") {
                    Button { targetDevice = "local"; targetOutput = "default"; selectedPC = nil } label: {
                        HStack { Label("Этот iPhone", systemImage: "iphone"); Spacer(); if targetDevice == "local" { Image(systemName: "checkmark") } }
                    }.disabled(store.busy)
                    ForEach(store.devices) { device in
                        Button {
                            selectedPC = device; targetDevice = "agent:" + device.name; targetOutput = "default"; loadingOutputs = true
                            store.run { defer { loadingOutputs = false }; try await store.loadOutputs(source: device.name) }
                        } label: {
                            HStack { Image(systemName: "desktopcomputer"); VStack(alignment: .leading) { Text(device.name); Text(device.online ? "В сети" : "Не в сети").font(.caption).foregroundStyle(.secondary) }; Spacer(); if targetDevice == "agent:" + device.name { Image(systemName: "checkmark") } }
                        }.disabled(!device.online || store.busy).accessibilityIdentifier("output-device-\(device.id)")
                    }
                }
                if let device = selectedPC {
                    Section("Аудиовыход · \(device.name)") {
                        if loadingOutputs { ProgressView("Получаю устройства Windows…") }
                        ForEach(store.outputs) { output in
                            Button { targetOutput = output.id } label: {
                                HStack { Label(output.name, systemImage: "speaker.wave.2"); Spacer(); if targetOutput == output.id { Image(systemName: "checkmark") } }
                            }.disabled(store.busy)
                        }
                    }
                }
                Section("AirPlay и Bluetooth") {
                    HStack { Text("Выбрать выход iPhone"); Spacer(); RoutePicker().frame(width: 44, height: 44) }
                }
                NativeMessage(store: store)
            }.navigationTitle("Где слушать").navigationBarTitleDisplayMode(.inline).toolbar { Button("Готово") { dismiss() } }
                .safeAreaInset(edge: .bottom) {
                    VStack(spacing: 14) {
                        Text("Продолжим с текущего места.").font(.footnote).foregroundStyle(.secondary)
                        Button { store.run { try await store.transfer(to: targetDevice, output: targetOutput); dismiss() } } label: { HStack { Spacer(); if store.busy { ProgressView() }; Text(store.busy ? "Переключаю…" : "Переключить"); Spacer() }.padding(.vertical, 12) }.buttonStyle(.borderedProminent).disabled(store.busy || loadingOutputs)
                    }.padding(20).background(XASSStyle.surface)
                }
                .onAppear { targetDevice = store.selectedDevice; targetOutput = store.outputID }
        }.presentationDetents([.fraction(0.7), .large]).presentationDragIndicator(.visible)
    }
}

@MainActor struct NativeDevicesView: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        NavigationStack {
            List {
                if !store.authorized { NativeLoginPrompt(store: store) }
                ForEach(store.devices) { device in
                    NavigationLink { NativeDeviceDetail(store: store, deviceID: device.id) } label: {
                        HStack(spacing: 14) {
                            Image(systemName: "desktopcomputer").font(.title2).frame(width: 40)
                            VStack(alignment: .leading, spacing: 4) { Text(device.name).font(.headline); HStack(spacing: 5) { Circle().fill(device.online ? Color.green : Color.gray).frame(width: 6, height: 6); Text(device.online ? "В сети" : "Не в сети").font(.caption).foregroundStyle(.secondary) } }
                        }.padding(.vertical, 6)
                    }.accessibilityIdentifier("native-device-\(device.id)")
                }
                if store.authorized && store.devices.isEmpty { ContentUnavailableView("Нет подключённых ПК", systemImage: "desktopcomputer", description: Text("Добавьте Windows-агент в Telegram Mini App и импортируйте его конфигурацию на ПК.")) }
                NativeMessage(store: store)
            }.navigationTitle("Устройства").refreshable { await store.refresh() }.safeAreaInset(edge: .bottom, spacing: 0) { NativeMiniPlayer(store: store) }
        }
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
    @State private var webAdmin = false
    @State private var forget = false
    var body: some View {
        NavigationStack {
            List {
                Section("Сервер") { Text(store.api.origin.url.absoluteString).textSelection(.enabled); Button("Обновить данные") { store.run { await store.refresh() } } }
                Section("Приложение") {
                    NavigationLink { NativeStorageView(store: store) } label: { Label("Хранение", systemImage: "externaldrive") }
                    Button { store.showEnrollment = true } label: { Label(store.enrolled ? "Защищённый ключ iPhone привязан" : "Привязать ключ iPhone", systemImage: "lock.shield") }
                    Label("Face ID или код-пароль при возвращении", systemImage: "faceid").font(.callout)
                }
                Section("Дополнительно") {
                    Button { webAdmin = true } label: { Label("Администрирование сайта · веб", systemImage: "safari") }
                    Text("Музыка, устройства и загрузки работают нативно. Только дополнительные настройки сайта открываются отдельной веб-страницей.").font(.caption).foregroundStyle(.secondary)
                }
                Section { Button("Выйти и сменить сервер", role: .destructive) { forget = true }; Text("XASS \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "")").foregroundStyle(.secondary) }
                NativeMessage(store: store)
            }.navigationTitle("Настройки").safeAreaInset(edge: .bottom, spacing: 0) { NativeMiniPlayer(store: store) }
                .sheet(isPresented: $webAdmin) { NavigationStack { WebContainer(app: app, origin: store.api.origin).navigationTitle("Сайт · веб-панель").navigationBarTitleDisplayMode(.inline).toolbar { Button("Готово") { webAdmin = false } } } }
                .confirmationDialog("Выйти из XASS на этом iPhone?", isPresented: $forget, titleVisibility: .visible) { Button("Выйти и сменить сервер", role: .destructive) { app.forgetServer() } } message: { Text("Данные сервера и сохранённые треки не удаляются.") }
        }
    }
}

@MainActor struct NativeDownloadsView: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var audio: AudioController
    @State private var remove: DownloadedTrack?
    var body: some View {
        NavigationStack {
            List {
                if audio.downloads.isEmpty { ContentUnavailableView("Музыка без интернета", systemImage: "arrow.down.circle", description: Text("Сохраните треки из плеера. Они доступны в этом приложении без сети.")) }
                if !audio.downloadIDs.isEmpty { ProgressView("Сохраняю треки: \(audio.downloadIDs.count)") }
                ForEach(audio.downloads) { track in
                    HStack(spacing: 12) {
                        Button {
                            if store.authorized { store.run { try await store.transfer(to: "local", trackID: track.id, startPosition: 0) } }
                            else { store.playOffline(track) }
                        } label: { HStack(spacing: 12) { TrackArtwork(store: store, trackID: track.id).frame(width: 44, height: 44); VStack(alignment: .leading, spacing: 3) { Text(track.title).foregroundStyle(.primary); Text(track.artist.isEmpty ? "На этом iPhone" : track.artist).font(.caption).foregroundStyle(.secondary) }; Spacer() } }.buttonStyle(.plain)
                        Button { remove = track } label: { Image(systemName: "ellipsis").frame(width: 40, height: 44) }.buttonStyle(.borderless).accessibilityLabel("Удалить локальную копию")
                    }
                }
                NativeMessage(store: store)
            }.navigationTitle("Загрузки").safeAreaInset(edge: .bottom, spacing: 0) { NativeMiniPlayer(store: store) }
                .confirmationDialog("Удалить только копию с iPhone? На сервере трек останется.", isPresented: Binding(get: { remove != nil }, set: { if !$0 { remove = nil } }), titleVisibility: .visible) {
                    Button("Удалить локальную копию", role: .destructive) { if let track = remove { audio.removeDownload(track) }; remove = nil }
                }
        }
    }
}
