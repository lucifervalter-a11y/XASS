import SwiftUI
import AVKit

@main @MainActor struct XASSApp: App {
    @StateObject private var app = AppState()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            RootView(app: app, audio: app.audio)
                .preferredColorScheme(.dark)
                .onChange(of: scenePhase) { _, phase in app.scene(phase) }
                .onReceive(NotificationCenter.default.publisher(for: UIApplication.willResignActiveNotification)) { _ in app.cover() }
                .onAppear { app.scene(scenePhase) }
        }
    }
}

@MainActor struct RootView: View {
    @ObservedObject var app: AppState
    @ObservedObject var audio: AudioController
    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let store = app.native {
                if app.hasUnlocked { NativeShell(app: app, store: store).id(store.api.origin.namespace) }
                if app.locked || app.privacyCovered { LockView(app: app).zIndex(10) }
            } else { ConnectView(app: app) }
        }
        .background(WindowPrivacyShield().frame(width: 0, height: 0))
        .sheet(isPresented: $audio.showRoutes) { RouteSheet().presentationDetents([.height(280)]) }
        .sheet(isPresented: $app.showSettings) { AppSettingsView(app: app) }
        .onChange(of: app.locked) { _, locked in
            if locked { audio.showRoutes = false; app.showSettings = false }
        }
    }
}

struct BrandMark: View {
    var body: some View {
        Image("XASSBrand").resizable().scaledToFit().frame(width: 104, height: 104)
            .background(Color.black, in: RoundedRectangle(cornerRadius: 30)).clipShape(RoundedRectangle(cornerRadius: 30))
            .overlay(RoundedRectangle(cornerRadius: 30).stroke(.white.opacity(0.13)))
    }
}

@MainActor struct ConnectView: View {
    @ObservedObject var app: AppState
    @State private var address = ""
    @State private var pair = ""
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                BrandMark().padding(.top, 36)
                VStack(alignment: .leading, spacing: 8) {
                    Text("Ваш сервер.\nВ кармане.").font(.system(size: 36, weight: .semibold, design: .rounded))
                    Text("Устройства, музыка и ваш сайт — в XASS.").foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 12) {
                    Text("Адрес XASS").font(.subheadline.weight(.semibold))
                    TextField("https://ваш-домен.ru", text: $address).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .padding(15).background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14)).accessibilityIdentifier("serverAddress")
                    Text("Одноразовая ссылка или ключ · необязательно").font(.caption).foregroundStyle(.secondary)
                    SecureField("Вставить из Telegram Mini App", text: $pair).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .padding(15).background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14)).accessibilityIdentifier("pairLink")
                }
                if let error = app.error { Text(error).font(.callout).foregroundStyle(.orange).accessibilityIdentifier("connectionError") }
                Button { app.connect(address: address, pair: pair); if app.origin != nil { pair = "" } } label: {
                    HStack { Text("Подключить XASS").fontWeight(.semibold); Spacer(); Image(systemName: "arrow.right") }.padding(.vertical, 10)
                }.buttonStyle(.borderedProminent).disabled(address.trimmingCharacters(in: .whitespaces).isEmpty || app.clearingSession).accessibilityIdentifier("connectServer")
                VStack(alignment: .leading, spacing: 10) {
                    Label("Как войти", systemImage: "key.horizontal").font(.subheadline.weight(.semibold))
                    Text("Откройте XASS в Telegram → Инструменты → iPhone и веб-приложение → создайте защищённую ссылку. Вставьте её здесь. Ключ одноразовый и не сохраняется.")
                    Text("Без ссылки откроется обычный вход через Telegram. Для защиты приложения на iPhone должен быть включён код-пароль.")
                }.font(.caption).foregroundStyle(.secondary).lineSpacing(4).padding(18).background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 18))
                Text("Только ваш HTTPS-сервер. Без общего облачного аккаунта XASS.").font(.caption2).foregroundStyle(.secondary)
            }.padding(24).frame(maxWidth: 500)
        }.scrollDismissesKeyboard(.interactively)
    }
}

@MainActor struct LockView: View {
    @ObservedObject var app: AppState
    var body: some View {
        VStack(spacing: 22) {
            Spacer(); BrandMark()
            Text("XASS защищён").font(.title2.weight(.semibold))
            Text("Подтвердите вход через Face ID\nили код-пароль iPhone.").multilineTextAlignment(.center).foregroundStyle(.secondary)
            if let error = app.error { Text(error).font(.callout).foregroundStyle(.orange).multilineTextAlignment(.center).padding(.horizontal, 28) }
            Button { app.authenticate() } label: { Label(app.authenticating ? "Проверяю…" : "Открыть XASS", systemImage: "faceid").padding(.vertical, 8).padding(.horizontal, 20) }
                .buttonStyle(.borderedProminent).disabled(app.authenticating)
            Spacer()
            Label("Музыка продолжает играть в фоне", systemImage: "waveform").font(.caption).foregroundStyle(.secondary).padding(.bottom, 28)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).background(Color.black).ignoresSafeArea()
    }
}

@MainActor struct DownloadsView: View {
    @ObservedObject var audio: AudioController
    @Environment(\.dismiss) private var dismiss
    @State private var removing: DownloadedTrack?
    var body: some View {
        NavigationStack {
            List {
                if audio.downloads.isEmpty {
                    ContentUnavailableView("Музыка без интернета", systemImage: "arrow.down.circle", description: Text("Сохраните треки из библиотеки XASS. Они останутся только в этом приложении, отдельно от файлов сервера."))
                }
                if !audio.downloadIDs.isEmpty { Label("Сохраняю треки: \(audio.downloadIDs.count)…", systemImage: "arrow.down").foregroundStyle(.secondary) }
                ForEach(audio.downloads) { track in
                    HStack {
                        Button { audio.playOffline(track) } label: {
                            HStack(spacing: 14) {
                                Image(systemName: audio.trackID == track.id && audio.state == "playing" ? "waveform" : "play.fill").frame(width: 26)
                                VStack(alignment: .leading) { Text(track.title).foregroundStyle(.primary); Text(track.artist.isEmpty ? "Сохранено на iPhone" : track.artist).font(.caption).foregroundStyle(.secondary) }
                            }
                        }.buttonStyle(.plain)
                        Spacer()
                        Button(role: .destructive) { removing = track } label: { Image(systemName: "trash").frame(width: 38, height: 44) }.buttonStyle(.borderless).accessibilityLabel("Удалить локальную копию \(track.title)")
                    }
                }
                if audio.trackID > 0 {
                    Section("Сейчас играет") {
                        Text(audio.title)
                        HStack {
                            Button { audio.state == "playing" ? audio.pause() : audio.resume() } label: { Label(audio.state == "playing" ? "Пауза" : "Слушать", systemImage: audio.state == "playing" ? "pause.fill" : "play.fill") }
                            Spacer(); Button("Стоп") { audio.stop() }
                        }.buttonStyle(.borderless)
                    }
                }
                if let error = audio.error { Text(error).foregroundStyle(.orange) }
            }.navigationTitle("Загрузки").toolbar { Button("Готово") { dismiss() } }
                .confirmationDialog("Удалить только копию с iPhone? На сервере трек останется.", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })) {
                    Button("Удалить локальную копию", role: .destructive) { if let track = removing { audio.removeDownload(track) }; removing = nil }
                }
        }
    }
}

@MainActor struct AppSettingsView: View {
    @ObservedObject var app: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var confirmForget = false
    var body: some View {
        NavigationStack {
            Form {
                Section("Сервер") { Text(app.origin?.url.absoluteString ?? "Не выбран").textSelection(.enabled) }
                Section("Защита") {
                    Label("Face ID или код-пароль при каждом возвращении", systemImage: "lock.shield")
                    Text("Ключ входа хранится в Keychain этого iPhone. При смене сервера управление и воспроизведение остановятся, вход нужно будет подтвердить заново.").font(.caption).foregroundStyle(.secondary)
                }
                Section {
                    Button("Отключить этот сервер", role: .destructive) { confirmForget = true }
                    Text("Файлы сервера и сохранённые треки не удаляются. После возврата к этому адресу загрузки появятся снова.").font(.caption).foregroundStyle(.secondary)
                }
                Section { Text("XASS \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "")").foregroundStyle(.secondary) }
            }.navigationTitle("Приложение").toolbar { Button("Готово") { dismiss() } }
                .confirmationDialog("Выйти из XASS на этом iPhone?", isPresented: $confirmForget) { Button("Выйти и сменить сервер", role: .destructive) { app.forgetServer() } }
        }
    }
}

struct RoutePicker: UIViewRepresentable {
    func makeUIView(context: Context) -> AVRoutePickerView {
        let picker = AVRoutePickerView(); picker.tintColor = .white; picker.activeTintColor = .systemBlue; picker.prioritizesVideoDevices = false
        return picker
    }
    func updateUIView(_ uiView: AVRoutePickerView, context: Context) {}
}
struct RouteSheet: View {
    var body: some View {
        VStack(spacing: 16) { Text("Куда вывести звук").font(.title3.weight(.semibold)); RoutePicker().frame(width: 72, height: 72); Text("Нажмите AirPlay, чтобы выбрать iPhone, наушники или доступную колонку.").font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center) }.padding(28)
    }
}

/// A window-level shield also covers presented sheets in the app-switcher snapshot.
struct WindowPrivacyShield: UIViewRepresentable {
    final class Host: UIView {
        private var overlay: UIView?
        private var observers: [NSObjectProtocol] = []
        override init(frame: CGRect) {
            super.init(frame: frame)
            observers.append(NotificationCenter.default.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { [weak self] _ in self?.cover() })
            observers.append(NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in self?.overlay?.removeFromSuperview(); self?.overlay = nil })
        }
        required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
        private func cover() {
            guard overlay == nil, let window = window else { return }
            let cover = UIView(frame: window.bounds); cover.backgroundColor = .black; cover.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            let label = UILabel(frame: cover.bounds); label.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            label.text = "XASS"; label.textColor = .white; label.font = .systemFont(ofSize: 28, weight: .semibold); label.textAlignment = .center
            cover.addSubview(label); window.addSubview(cover); overlay = cover
        }
        deinit { observers.forEach(NotificationCenter.default.removeObserver); overlay?.removeFromSuperview() }
    }
    func makeUIView(context: Context) -> Host { Host(frame: .zero) }
    func updateUIView(_ uiView: Host, context: Context) {}
}
