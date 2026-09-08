import SwiftUI
import UniformTypeIdentifiers

@MainActor struct NativeStorageView: View {
    @ObservedObject var store: NativeStore
    var source: String? = nil
    @State private var importFiles = false
    @State private var info: String?
    @State private var folderImport = false
    @State private var clearCache = false
    var body: some View {
        List {
            Section("На iPhone") {
                Picker("Автоматический кэш", selection: Binding(get: { store.audio.cacheLimitMB }, set: { limit in store.run { try await store.audio.setCacheLimit(limit) } })) {
                    Text("Выключен").tag(0); Text("256 МБ").tag(256); Text("512 МБ").tag(512); Text("1 ГБ").tag(1024)
                }.disabled(store.audio.cacheWorking)
                HStack { Label("Музыка в кэше", systemImage: "music.note"); Spacer(); Text(ByteCountFormatter.string(fromByteCount: store.audio.cacheBytes, countStyle: .file)).foregroundStyle(.secondary) }
                Button("Очистить кэш музыки", role: .destructive) { clearCache = true }.disabled(store.audio.cacheWorking)
                HStack { Label("Кэш обложек", systemImage: "externaldrive"); Spacer(); Text("В памяти").foregroundStyle(.secondary) }
                Button { store.clearArtworkCache() } label: { Label("Очистить кэш обложек", systemImage: "trash") }
                HStack { Label("Скачано вручную", systemImage: "doc"); Spacer(); Text("\(store.audio.downloads.count) треков").foregroundStyle(.secondary) }
                Text("Кэш сохраняет трек после двух запусков. При заполнении удаляется давно не звучавшая музыка; ручные загрузки не затрагиваются. Все данные привязаны к этому серверу.").font(.caption).foregroundStyle(.secondary)
            }
            Section {
                ForEach(store.tracks) { track in
                    NavigationLink { NativeTrackStorageView(store: store, track: track, initialSource: source) } label: { HStack { Text(track.title).lineLimit(1); Spacer(); Image(systemName: "music.note").foregroundStyle(.secondary) } }
                }
                if store.tracks.isEmpty { Text("Добавьте треки в библиотеку, чтобы выбрать место хранения.").foregroundStyle(.secondary) }
            } header: { Text(source.map { "На компьютере · " + $0 } ?? "Копии на компьютере") } footer: { Text("Оригинал удаляется с сервера только после проверки копии и вашего подтверждения.") }
            Section("Импорт") {
                Button { importFiles = true } label: { Label("Из файлов или ZIP", systemImage: "doc") }.disabled(store.uploadName != nil)
                if source != nil { Button { folderImport = true } label: { Label("Импортировать папку ПК", systemImage: "folder") } }
                Button { info = "Откройте своего бота XASS и отправьте аудиофайлы или ZIP. Бот добавит поддерживаемые треки в вашу библиотеку; после завершения обновите список на iPhone." } label: { Label("Из Telegram", systemImage: "paperplane") }
                Button { info = "Доступ VK Музыки не настроен. XASS не выдаёт обычный вход VK за доступ к полным аудиозаписям. Вы можете импортировать принадлежащие вам аудиофайлы или ZIP." } label: { HStack { Text("VK Музыка"); Spacer(); Text("Доступ не настроен").font(.caption).foregroundStyle(.secondary) } }
            }
            NativeMessage(store: store)
        }.navigationTitle("Хранение")
            .fileImporter(isPresented: $importFiles, allowedContentTypes: [.audio, .zip], allowsMultipleSelection: true) { result in switch result { case .success(let urls): store.run { for url in urls { try await store.upload(url) } }; case .failure(let error): store.handle(error) } }
            .alert("Импорт музыки", isPresented: Binding(get: { info != nil }, set: { if !$0 { info = nil } })) { Button("Понятно", role: .cancel) {} } message: { Text(info ?? "") }
            .confirmationDialog("Очистить автоматический кэш музыки?", isPresented: $clearCache, titleVisibility: .visible) {
                Button("Очистить кэш", role: .destructive) { store.run { try await store.audio.clearAutomaticCache(); store.notice = "Автоматический кэш очищен. Ручные загрузки сохранены." } }
                Button("Отмена", role: .cancel) {}
            } message: { Text("Сохранённые вручную треки и файлы на сервере не удаляются.") }
            .sheet(isPresented: $folderImport) { if let source = source { NativeFolderImportView(store: store, source: source) } }
    }
}

struct NativeStorageCopy: Identifiable {
    let trackID: Int
    let source: String
    let sha256: String
    let online: Bool
    let attached: Bool
    var id: String { "\(trackID):\(source)" }
    init?(_ value: [String: Any]) {
        guard let id = value["track_id"] as? Int, let source = value["source_name"] as? String, let sha = value["sha256"] as? String else { return nil }
        trackID = id; self.source = source; sha256 = sha; online = value["online"] as? Bool ?? false; attached = value["attached"] as? Bool ?? false
    }
}

@MainActor struct NativeTrackStorageView: View {
    @ObservedObject var store: NativeStore
    let track: LibraryTrack
    var initialSource: String? = nil
    @State private var copies: [NativeStorageCopy] = []
    @State private var targets: [String] = []
    @State private var available: Bool?
    @State private var jobs: [String] = []
    @State private var working = false
    @State private var evict: NativeStorageCopy?
    var body: some View {
        List {
            Section("На сервере") {
                Label(available == true ? "Оригинал доступен" : available == false ? "Оригинал на компьютере" : "Проверяю состояние…", systemImage: "server.rack")
                if available == false { Button("Восстановить копию на сервер") { operate { let response = try await store.api.request("/api/mini/music/storage/\(track.id)/restore", method: "POST", body: [:]); store.notice = response["status"] as? String == "available" ? "Трек доступен на сервере" : "Восстановление запрошено. Обновите состояние после ответа ПК." } } }
            }
            Section("Проверенные копии") {
                ForEach(copies) { copy in
                    VStack(alignment: .leading, spacing: 8) {
                        Label(copy.source, systemImage: "desktopcomputer").font(.headline)
                        Text(copy.online && copy.attached ? "ПК в сети" : "ПК недоступен").font(.caption).foregroundStyle(.secondary)
                        Text("SHA-256 · " + String(copy.sha256.prefix(16)) + "…").font(.caption2.monospaced()).foregroundStyle(.secondary)
                        Button("Проверить копию заново") { replicate(copy.source) }.disabled(!copy.online || !copy.attached)
                        if available == true { Button("Освободить копию на сервере", role: .destructive) { evict = copy }.disabled(!copy.online || !copy.attached) }
                    }.padding(.vertical, 6)
                }
                if copies.isEmpty { Text("Проверенных копий на ПК пока нет.").foregroundStyle(.secondary) }
            }
            Section("Скопировать на ПК") {
                ForEach(targets, id: \.self) { source in Button(source) { replicate(source) } }
                if targets.isEmpty { Text("Нужен подключённый агент с поддержкой музыкального хранилища.").foregroundStyle(.secondary) }
            }
            if !jobs.isEmpty { Section("Операции") { ForEach(Array(jobs.enumerated()), id: \.offset) { _, text in Text(text).font(.callout) } } }
            NativeMessage(store: store)
        }.navigationTitle(track.title).navigationBarTitleDisplayMode(.inline).disabled(working || store.busy)
            .refreshable { await load() }.task { await load() }
            .toolbar { Button { Task { await load() } } label: { Image(systemName: "arrow.clockwise") } }
            .confirmationDialog("Удалить только серверную копию?", isPresented: Binding(get: { evict != nil }, set: { if !$0 { evict = nil } }), titleVisibility: .visible) {
                Button("Освободить копию на сервере", role: .destructive) { if let copy = evict { operate { try await store.freeServerCopy(trackID: track.id, source: copy.source, sha256: copy.sha256) } }; evict = nil }
            } message: { Text("Музыка будет храниться на выбранном ПК. Сервер ещё раз проверит, что ПК в сети и его копия недавно подтверждена. Потребуется Face ID или код-пароль.") }
    }
    private func operate(_ action: @escaping () async throws -> Void) {
        guard !working else { return }; working = true
        store.run { defer { working = false }; try await action(); await load() }
    }
    private func replicate(_ source: String) {
        operate { _ = try await store.api.request("/api/mini/music/storage/\(track.id)/replicate", method: "POST", body: ["source_name": source]); store.notice = "Копирование запрошено. Оригинал на сервере сохранён." }
    }
    private func load() async {
        do {
            let value = try await store.api.request("/api/mini/music/storage?track_id=\(track.id)", method: "GET", body: nil)
            available = value["server_available"] as? Bool
            copies = (value["copies"] as? [[String: Any]] ?? []).compactMap(NativeStorageCopy.init)
            targets = (value["targets"] as? [[String: Any]] ?? []).filter { $0["online"] as? Bool == true && $0["supported"] as? Bool == true }.compactMap { $0["source_name"] as? String }
            jobs = (value["jobs"] as? [[String: Any]] ?? []).map { job in
                let status = job["status"] as? String ?? "pending", operation = job["operation"] as? String == "restore" ? "Восстановление" : "Копирование"
                let labels = ["pending": "в очереди", "transferring": "передача", "complete": "завершено", "completed": "завершено", "failed": "ошибка", "cancelled": "отменено"]
                return operation + " · " + (labels[status] ?? status)
            }
        } catch { store.handle(error) }
    }
}

@MainActor struct NativeFolderImportView: View {
    @ObservedObject var store: NativeStore
    let source: String
    @State private var root = "downloads"
    @State private var path = ""
    @State private var confirm = false
    @State private var importID: String?
    @State private var status = ""
    @State private var working = false
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            Form {
                Section("Папка на \(source)") {
                    Picker("Расположение", selection: $root) { Text("Загрузки").tag("downloads"); Text("Музыка в документах").tag("documents"); Text("Рабочий стол").tag("desktop"); Text("Файлы XASS").tag("xass_files") }
                    TextField("Подпапка, например Music/Albums", text: $path).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Text("Пустое поле означает выбранную папку целиком. XASS импортирует аудио из неё и вложенных папок; оригиналы на ПК сохранятся. Произвольный путь диска не используется.").font(.footnote).foregroundStyle(.secondary)
                }.disabled(importID != nil)
                if let id = importID {
                    Section("Состояние") { Text(status); Button("Обновить состояние") { poll(id) }; Button("Отменить импорт", role: .destructive) { execute { _ = try await store.api.request("/api/mini/music/storage/imports/" + OwnerAPI.pathComponent(id), method: "DELETE", body: nil); status = "Импорт отменён. Файлы не удалены." } } }
                } else { Button("Импортировать папку") { confirm = true }.disabled(working) }
                NativeMessage(store: store)
            }.navigationTitle("Импорт с ПК").navigationBarTitleDisplayMode(.inline).toolbar { Button("Готово") { dismiss() } }
                .confirmationDialog("Импортировать аудио из выбранной папки \(source)?", isPresented: $confirm, titleVisibility: .visible) {
                    Button("Импортировать") { execute { let response = try await store.api.request("/api/mini/music/storage/import-directory", method: "POST", body: ["source_name": source, "root": root, "path": path, "confirm": "IMPORT DIRECTORY"]); guard let id = response["import_id"] as? String else { throw OwnerAPIError.invalidResponse }; importID = id; status = "Ожидаю ответ агента. Оригиналы на ПК сохраняются." } }
                }
        }
    }
    private func execute(_ action: @escaping () async throws -> Void) { working = true; store.run { defer { working = false }; try await action() } }
    private func poll(_ id: String) {
        execute {
            let response = try await store.api.request("/api/mini/music/storage/imports/" + OwnerAPI.pathComponent(id), method: "GET", body: nil)
            let result = response["result"] as? [String: Any] ?? [:], code = response["status"] as? String ?? "pending"
            let labels = ["pending": "В очереди", "complete": "Завершено", "partial": "Завершено с пропусками", "cancelled": "Отменено"]
            let count = (result["tracks"] as? [Any])?.count ?? result["tracks"] as? Int ?? 0
            status = (labels[code] ?? code) + ". Треков: \(count)."
            if ["complete", "partial"].contains(code) { await store.refresh() }
        }
    }
}
