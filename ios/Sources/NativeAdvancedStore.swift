import Foundation
import Combine
import UIKit
import UniformTypeIdentifiers

struct NativeTimelineEvent: Identifiable {
    let id: String
    let title: String
    let message: String
    let date: String
    let level: String
    let device: String
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id; title = value["title"] as? String ?? "Событие"
        message = value["message"] as? String ?? ""; date = value["created_at"] as? String ?? ""
        level = value["level"] as? String ?? "info"; device = value["device"] as? String ?? ""
    }
}

struct NativeAutomationRule: Identifiable {
    var id = UUID().uuidString.lowercased()
    var name = ""
    var condition = "agent_offline"
    var device = ""
    var service = ""
    var threshold = 5.0
    var duration = 0
    var cooldown = 60
    var priority = "warning"
    var enabled = true
    static let conditions = ["agent_offline", "cpu_high", "disk_low", "service_down", "agent_outdated"]
    static func label(_ condition: String) -> String {
        ["agent_offline": "ПК не в сети", "cpu_high": "Высокая загрузка CPU", "disk_low": "Мало места на диске", "service_down": "Сервис остановлен", "agent_outdated": "Устаревший агент"][condition] ?? condition
    }
    init(_ value: [String: Any] = [:]) {
        id = value["id"] as? String ?? id; name = value["name"] as? String ?? name
        condition = value["condition"] as? String ?? condition; device = value["device"] as? String ?? ""
        service = value["service"] as? String ?? ""; threshold = NativeValue.number(value["threshold"], fallback: threshold)
        duration = value["duration_minutes"] as? Int ?? 0; cooldown = value["cooldown_minutes"] as? Int ?? 60
        priority = value["priority"] as? String ?? "warning"; enabled = value["enabled"] as? Bool ?? true
    }
    var payload: [String: Any] {
        ["id": id, "name": name.trimmingCharacters(in: .whitespacesAndNewlines), "condition": condition,
         "device": device, "service": service, "threshold": threshold, "duration_minutes": duration,
         "cooldown_minutes": cooldown, "priority": priority, "enabled": enabled]
    }
}

struct NativeScenario: Identifiable {
    var id = UUID().uuidString.lowercased()
    var name = ""
    var icon = "bolt"
    var color = "#376dff"
    var actions: [String] = []
    var devices: [String] = []
    var delay = 0
    var schedule = ""
    var enabled = true
    var builtin = false
    static let availableActions = ["away_on", "away_off", "quiet_on", "quiet_off", "check_all", "lock_all", "update_all"]
    static func label(_ action: String) -> String {
        ["away_on": "Включить «Не в сети»", "away_off": "Выключить «Не в сети»", "quiet_on": "Включить тихие часы", "quiet_off": "Выключить тихие часы", "check_all": "Проверить устройства", "lock_all": "Заблокировать ПК", "update_all": "Обновить агенты"][action] ?? action
    }
    init(_ value: [String: Any] = [:]) {
        id = value["id"] as? String ?? id; name = value["name"] as? String ?? name
        icon = value["icon"] as? String ?? icon; color = value["color"] as? String ?? color
        actions = value["actions"] as? [String] ?? []; devices = value["devices"] as? [String] ?? []
        delay = value["delay_sec"] as? Int ?? 0; schedule = value["schedule"] as? String ?? ""
        enabled = value["enabled"] as? Bool ?? true; builtin = value["builtin"] as? Bool ?? false
    }
    var dangerous: Bool { actions.contains("lock_all") || actions.contains("update_all") }
    var binding: [String: Any] { ["scenario_id": id, "actions": actions, "devices": devices, "delay_sec": delay] }
    var payload: [String: Any] {
        ["id": id, "name": name.trimmingCharacters(in: .whitespacesAndNewlines), "icon": icon, "color": color,
         "actions": actions, "devices": devices, "delay_sec": delay, "schedule": schedule, "enabled": enabled]
    }
    var valid: Bool {
        !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !actions.isEmpty &&
        (schedule.isEmpty || schedule.range(of: #"^(?:[01]\d|2[0-3]):[0-5]\d$"#, options: .regularExpression) != nil)
    }
}

struct NativeConversation: Identifiable {
    let id: Int64
    let title: String
    let text: String
    let count: Int
    let pinned: Bool
    init?(_ value: [String: Any]) {
        guard let id = value["chat_id"] as? NSNumber else { return nil }
        self.id = id.int64Value; title = value["title"] as? String ?? String(id.int64Value)
        text = value["last_text"] as? String ?? ""; count = value["count"] as? Int ?? 0
        pinned = value["pinned"] as? Bool ?? false
    }
}

struct NativeArchivedMessage: Identifiable {
    let id: Int
    let sender: String
    let text: String
    let date: String
    let deleted: Bool
    let edited: Bool
    let reply: String?
    let revisions: [[String: Any]]
    let media: [[String: Any]]
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? Int, id > 0 else { return nil }
        self.id = id; sender = value["from_username"] as? String ?? ""
        text = value["text"] as? String ?? ""; date = value["date"] as? String ?? ""
        deleted = value["deleted"] as? Bool ?? false; edited = value["edited"] as? Bool ?? false
        reply = (value["reply"] as? [String: Any])?["text"] as? String
        revisions = value["revisions"] as? [[String: Any]] ?? []; media = value["media"] as? [[String: Any]] ?? []
    }
}

struct NativeRemoteFile: Identifiable {
    var id: String { name }
    let name: String
    let directory: Bool
    let size: Int64
    init?(_ value: [String: Any]) {
        guard let name = value["name"] as? String, !name.isEmpty, name != ".", name != "..",
              !name.contains("/"), !name.contains("\\"), !name.contains(":"), !name.contains("\0") else { return nil }
        self.name = name; directory = value["type"] as? String == "directory"; size = (value["size"] as? NSNumber)?.int64Value ?? 0
    }
}

struct NativeRemoteFileTarget: Identifiable {
    var id: String { root + "/" + path }
    let root: String
    let folder: String
    let name: String
    let path: String
    init?(root: String, folder: String, file: NativeRemoteFile) {
        guard !file.directory, Self.validFolder(root: root, path: folder) else { return nil }
        self.root = root; self.folder = folder; name = file.name
        path = folder.isEmpty ? file.name : folder + "/" + file.name
    }
    static func validFolder(root: String, path: String) -> Bool {
        guard ["downloads", "desktop", "documents", "xass_files"].contains(root), !path.contains("\\"), !path.contains(":"), !path.contains("\0") else { return false }
        return path.isEmpty || path.split(separator: "/", omittingEmptySubsequences: false).allSatisfy { !$0.isEmpty && $0 != "." && $0 != ".." }
    }
    var payload: [String: Any] { ["root": root, "path": path] }
    func binding(device: NativeDevice) -> [String: Any] { ["source_id": device.id, "command": "file_delete", "payload": payload] }
}

@MainActor final class NativeAdvancedStore: ObservableObject {
    let owner: NativeStore
    @Published var busy = false
    @Published var error: String?
    @Published var status: String?
    @Published var events: [NativeTimelineEvent] = []
    @Published var rules: [NativeAutomationRule] = []
    @Published var scenarios: [NativeScenario] = []
    @Published var chats: [NativeConversation] = []
    @Published var messages: [NativeArchivedMessage] = []
    @Published var hasMore = false
    @Published var screenshot: UIImage?
    @Published var files: [NativeRemoteFile] = []
    @Published var currentRoot = "downloads"
    @Published var currentPath = ""
    @Published var filesLoaded = false
    @Published var filesTruncated = false
    @Published var clipboard = ""
    @Published var commands: [[String: Any]] = []
    @Published var previewURL: URL?
    private var pendingCommandID: Int?
    private var peerKey: [String: Any]?
    private var messageRequest: String?
    private var temporaryFiles: [URL] = []
    private let workspacePrivateKey: () -> Data?
    init(owner: NativeStore, workspacePrivateKey: (() -> Data?)? = nil) {
        self.owner = owner
        let keyName = NativeWorkspaceCrypto.keyName(owner.api.origin)
        self.workspacePrivateKey = workspacePrivateKey ?? { SecureStore.load(keyName) }
    }

    func perform(_ action: () async throws -> Void) async {
        guard !busy else { return }; busy = true; error = nil; status = nil
        defer { busy = false }
        do { try await action() }
        catch is CancellationError {}
        catch { self.error = error.localizedDescription; if let failure = error as? OwnerAPIError, [401, 428].contains(failure.status) { owner.handle(failure) } }
    }
    func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> [String: Any] {
        try await owner.api.request(path, method: method, body: body)
    }
    static func query(_ path: String, _ items: [URLQueryItem]) -> String {
        var parts = URLComponents(); parts.queryItems = items
        return path + "?" + (parts.percentEncodedQuery ?? "").replacingOccurrences(of: "+", with: "%2B")
    }
    static func date(_ raw: String) -> String {
        let parser = ISO8601DateFormatter(); parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let first = parser.date(from: raw) ?? parser.date(from: raw + "Z"); parser.formatOptions = [.withInternetDateTime]
        guard let date = first ?? parser.date(from: raw) ?? parser.date(from: raw + "Z") else { return raw }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
    func loadTimeline(type: String, level: String, device: String) async throws {
        let result = try await request(Self.query("/api/mini/timeline", [.init(name: "limit", value: "200"), .init(name: "event_type", value: type), .init(name: "level", value: level), .init(name: "device", value: device)]))
        events = (result["items"] as? [[String: Any]] ?? []).compactMap(NativeTimelineEvent.init)
    }
    func loadRules() async throws { rules = (try await request("/api/mini/rules")["rules"] as? [[String: Any]] ?? []).map(NativeAutomationRule.init) }
    func saveRule(_ rule: NativeAutomationRule) async throws {
        let result = try await request("/api/mini/rules", method: "POST", body: rule.payload)
        rules = (result["rules"] as? [[String: Any]] ?? []).map(NativeAutomationRule.init); status = "Правило сохранено"
    }
    func deleteRule(_ rule: NativeAutomationRule) async throws {
        let result = try await request("/api/mini/rules/" + OwnerAPI.pathComponent(rule.id), method: "DELETE")
        rules = (result["rules"] as? [[String: Any]] ?? []).map(NativeAutomationRule.init)
    }
    func loadScenarios() async throws { scenarios = (try await request("/api/mini/scenarios")["scenarios"] as? [[String: Any]] ?? []).map(NativeScenario.init) }
    func saveScenario(_ scenario: NativeScenario) async throws {
        guard scenario.valid else { throw OwnerAPIError(status: 0, message: "Укажите название, действия и время в формате ЧЧ:ММ либо оставьте время пустым.") }
        let result = try await request("/api/mini/scenarios", method: "POST", body: scenario.payload)
        scenarios = (result["scenarios"] as? [[String: Any]] ?? []).map(NativeScenario.init)
    }
    func deleteScenario(_ scenario: NativeScenario) async throws {
        let result = try await request("/api/mini/scenarios/" + OwnerAPI.pathComponent(scenario.id), method: "DELETE")
        scenarios = (result["scenarios"] as? [[String: Any]] ?? []).map(NativeScenario.init)
    }
    func runScenario(_ scenario: NativeScenario) async throws {
        guard scenario.enabled else { throw OwnerAPIError(status: 409, message: "Сценарий выключен") }
        guard !owner.busy else { throw OwnerAPIError(status: 409, message: "Дождитесь завершения текущего действия.") }
        owner.busy = true; defer { owner.busy = false; owner.confirmationActivity?(false) }
        var body: [String: Any] = [:]
        if scenario.dangerous {
            owner.confirmationActivity?(true)
            body["action_proof"] = try await owner.authorization.proof(purpose: "scenario:" + scenario.id, binding: scenario.binding, reason: "Запустить сценарий «\(scenario.name)»")
            for _ in 0..<20 {
                if owner.canSendActions() { break }
                try await Task.sleep(for: .milliseconds(100))
            }
        }
        try Task.checkCancellation()
        guard owner.canSendActions() else { throw OwnerAPIError(status: 0, message: "Вернитесь в XASS и повторите действие.") }
        let result = try await request("/api/mini/scenarios/" + OwnerAPI.pathComponent(scenario.id) + "/run", method: "POST", body: body)
        let count = (result["commands"] as? [[String: Any]] ?? []).count
        let skipped = result["skipped_offline"] as? [String] ?? []
        status = "Сценарий запущен. Команд в очереди: \(count)." + (skipped.isEmpty ? "" : " Не в сети: " + skipped.joined(separator: ", "))
    }
    func loadChats() async throws { chats = (try await request("/api/mini/conversations")["chats"] as? [[String: Any]] ?? []).compactMap(NativeConversation.init) }
    func pin(_ chat: NativeConversation) async throws {
        _ = try await request("/api/mini/conversations/\(chat.id)/pin", method: "POST", body: ["pinned": !chat.pinned]); try await loadChats()
    }
    func loadMessages(chat: Int64, filter: String, earlier: Bool = false) async throws {
        let key = "\(chat):\(filter)"
        if !earlier { messageRequest = key; messages = []; hasMore = false }
        guard messageRequest == key else { return }
        var query: [URLQueryItem] = [.init(name: "limit", value: "60")]
        if !filter.isEmpty { query.append(.init(name: filter + "_only", value: "true")) }
        if earlier, let oldest = messages.map(\.id).min() { query.append(.init(name: "before", value: String(oldest))) }
        let result = try await request(Self.query("/api/mini/conversations/\(chat)", query))
        guard messageRequest == key else { return }
        let incoming = (result["messages"] as? [[String: Any]] ?? []).compactMap(NativeArchivedMessage.init)
        var seen = Set<Int>(); messages = (incoming + (earlier ? messages : [])).filter { seen.insert($0.id).inserted }.sorted { $0.id < $1.id }
        hasMore = result["has_more"] as? Bool ?? false
    }
    func previewAttachment(_ media: [String: Any]) async throws {
        guard let id = media["id"] as? Int, id > 0 else { throw OwnerAPIError.invalidResponse }
        if let size = media["size"] as? Int, size > 16 * 1024 * 1024 { throw OwnerAPIError(status: 413, message: "Встроенный просмотр поддерживает файлы до 16 МБ.") }
        let bytes = try await owner.api.binary("/api/mini/media/\(id)")
        let mime = media["mime_type"] as? String ?? ""
        let suffix = ["image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "video/mp4": "mp4", "audio/mpeg": "mp3", "audio/ogg": "ogg", "application/pdf": "pdf", "text/plain": "txt"][mime] ?? "bin"
        try preview(bytes, filename: "Вложение-\(id).\(suffix)")
    }
    private func preview(_ bytes: Data, filename: String) throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("xass-preview-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let safeName = URL(fileURLWithPath: filename).lastPathComponent
        let url = folder.appendingPathComponent(safeName.isEmpty ? "file.bin" : safeName)
        try bytes.write(to: url, options: [.atomic, .completeFileProtection]); temporaryFiles.append(folder); previewURL = url
    }
    func clearPreviews() { previewURL = nil; for url in temporaryFiles { try? FileManager.default.removeItem(at: url) }; temporaryFiles = [] }
    func devicePath(_ device: NativeDevice) -> String { "/api/mini/agents/" + OwnerAPI.pathComponent(device.name) }
    func loadCommands(_ device: NativeDevice) async throws { commands = (try await request(devicePath(device) + "/commands?limit=50")["commands"] as? [[String: Any]] ?? []) }
    func cancelCommand(_ device: NativeDevice, id: Int) async throws {
        _ = try await request(devicePath(device) + "/commands/\(id)", method: "DELETE"); try await loadCommands(device)
    }
    /// Match the returned command ID. An older successful command is never used as the new result.
    func command(_ device: NativeDevice, name: String, payload: [String: Any] = [:]) async throws -> [String: Any] {
        guard ["screenshot", "files_list", "file_download", "clipboard_get", "clipboard_set"].contains(name) else { throw OwnerAPIError.invalidResponse }
        let result = try await request(devicePath(device) + "/commands", method: "POST", body: ["command": name, "payload": payload])
        return try await waitForCommand(device, response: result)
    }
    private func waitForCommand(_ device: NativeDevice, response result: [String: Any]) async throws -> [String: Any] {
        guard let item = result["command"] as? [String: Any], let id = item["id"] as? Int, id > 0 else { throw OwnerAPIError.invalidResponse }
        pendingCommandID = id; status = "Команда №\(id) отправлена. Жду ответ ПК…"
        defer { pendingCommandID = nil }
        for _ in 0..<30 {
            try Task.checkCancellation(); try await loadCommands(device)
            if let row = commands.first(where: { $0["id"] as? Int == id }) {
                let state = row["status"] as? String ?? ""
                let result = row["result"] as? [String: Any] ?? [:]
                if ["failed", "cancelled", "expired"].contains(state) || (state == "completed" && result["ok"] as? Bool != true) {
                    throw OwnerAPIError(status: 0, message: result["message"] as? String ?? "ПК не выполнил команду (\(state)).")
                }
                if state == "completed" {
                    status = result["message"] as? String ?? "Готово"
                    return result["details"] as? [String: Any] ?? [:]
                }
            }
            try await Task.sleep(for: .seconds(1))
        }
        throw OwnerAPIError(status: 0, message: "ПК ещё не ответил на команду №\(id). Проверьте историю команд: она может выполниться позже.")
    }
    private func optionalPeer(_ device: NativeDevice) async throws -> [String: Any]? {
        if let peerKey { return peerKey }
        let result = try await request("/api/mini/bootstrap")
        let source = (result["sources"] as? [[String: Any]] ?? []).first { $0["id"] as? Int == device.id && $0["source_name"] as? String == device.name }
        guard source != nil else { throw OwnerAPIError(status: 404, message: "ПК больше не привязан к серверу.") }
        guard let payload = source?["last_payload"] as? [String: Any], let raw = payload["e2e_public_jwk"], !(raw is NSNull) else { return nil }
        guard let key = raw as? [String: Any] else { throw OwnerAPIError.invalidResponse }
        _ = try NativeWorkspaceCrypto.publicKey(key); peerKey = key; return key
    }
    private func peer(_ device: NativeDevice) async throws -> [String: Any] {
        guard let key = try await optionalPeer(device) else { throw OwnerAPIError(status: 0, message: "ПК не опубликовал ключ шифрования. Обновите агент и повторите.") }
        return key
    }
    func capture(_ device: NativeDevice) async throws {
        screenshot = nil
        let result = try await command(device, name: "screenshot")
        guard let token = result["asset_token"] as? String, token.range(of: #"^[A-Za-z0-9_-]{20,80}$"#, options: .regularExpression) != nil else { throw OwnerAPIError.invalidResponse }
        var bytes = try await owner.api.binary(devicePath(device) + "/assets/" + token)
        if NativeWorkspaceCrypto.isSealed(bytes) { bytes = try NativeWorkspaceCrypto.open(bytes, origin: owner.api.origin, peer: try await peer(device), purpose: "screenshot") }
        guard let image = UIImage(data: bytes) else { throw OwnerAPIError.invalidResponse }; screenshot = image
    }
    func listFiles(_ device: NativeDevice, root: String, path: String = "") async throws {
        let result = try await command(device, name: "files_list", payload: ["root": root, "path": path])
        guard result["root"] as? String == root, result["path"] as? String == path else { throw OwnerAPIError.invalidResponse }
        var seen = Set<String>(); files = (result["entries"] as? [[String: Any]] ?? []).compactMap(NativeRemoteFile.init).filter { seen.insert($0.name).inserted }
        currentRoot = root; currentPath = path; filesLoaded = true; filesTruncated = result["truncated"] as? Bool ?? false
    }
    func download(_ device: NativeDevice, file: NativeRemoteFile) async throws {
        guard !file.directory, file.size <= 16 * 1024 * 1024 - 33 else { throw OwnerAPIError(status: 413, message: "Встроенный просмотр поддерживает файлы до 16 МБ.") }
        let path = currentPath.isEmpty ? file.name : currentPath + "/" + file.name
        let result = try await command(device, name: "file_download", payload: ["root": currentRoot, "path": path])
        guard let token = result["asset_token"] as? String, token.range(of: #"^[A-Za-z0-9_-]{20,80}$"#, options: .regularExpression) != nil else { throw OwnerAPIError.invalidResponse }
        var bytes = try await owner.api.binary(devicePath(device) + "/assets/" + token)
        if NativeWorkspaceCrypto.isSealed(bytes) { bytes = try NativeWorkspaceCrypto.open(bytes, origin: owner.api.origin, peer: try await peer(device), purpose: "file_download") }
        try preview(bytes, filename: file.name)
    }
    func deleteFile(_ device: NativeDevice, target: NativeRemoteFileTarget) async throws {
        guard !owner.busy else { throw OwnerAPIError(status: 409, message: "Дождитесь завершения текущего действия.") }
        owner.busy = true; owner.confirmationActivity?(true)
        defer { owner.busy = false; owner.confirmationActivity?(false) }
        let proof = try await owner.authorization.proof(purpose: "agent:file_delete:" + device.name, binding: target.binding(device: device), reason: "Удалить «\(target.name)» с ПК \(device.name)")
        for _ in 0..<20 {
            if owner.canSendActions() { break }; try await Task.sleep(for: .milliseconds(100))
        }
        try Task.checkCancellation()
        guard owner.canSendActions() else { throw OwnerAPIError(status: 0, message: "Вернитесь в XASS и повторите удаление.") }
        let response = try await request(devicePath(device) + "/commands", method: "POST", body: ["command": "file_delete", "payload": target.payload, "action_proof": proof])
        _ = try await waitForCommand(device, response: response)
        if currentRoot == target.root && currentPath == target.folder { files.removeAll { $0.name == target.name } }
        status = "Файл «\(target.name)» удалён с ПК"
    }
    func uploadFile(_ device: NativeDevice, url: URL, root: String, path: String) async throws {
        guard NativeRemoteFileTarget.validFolder(root: root, path: path) else { throw OwnerAPIError.invalidResponse }
        let scoped = url.startAccessingSecurityScopedResource(); defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let values = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .contentTypeKey])
        guard values.isRegularFile == true, let size = values.fileSize, size > 0, size <= 16 * 1024 * 1024 - 33 else {
            throw OwnerAPIError(status: 413, message: "Выберите непустой файл до 16 МБ.")
        }
        var bytes = try Data(contentsOf: url, options: .mappedIfSafe)
        guard bytes.count == size else { throw OwnerAPIError(status: 409, message: "Файл изменился во время чтения. Повторите выбор.") }
        let mime = values.contentType?.preferredMIMEType ?? "application/octet-stream"
        var headers = ["Content-Type": mime]
        if let key = try await optionalPeer(device) {
            guard let secret = workspacePrivateKey() else { throw NativeWorkspaceCrypto.missingKey }
            bytes = try NativeWorkspaceCrypto.seal(bytes, privateKey: secret, peer: key, purpose: "file_upload")
            headers = ["Content-Type": "application/x-xass-sealed", "X-XASS-Cipher": "xass-sealed-v1", "X-XASS-Inner-Type": mime]
        }
        try Task.checkCancellation()
        status = "Отправляю файл «\(url.lastPathComponent)»…"
        let uploadPath = Self.query(devicePath(device) + "/files/upload", [.init(name: "root", value: root), .init(name: "path", value: path), .init(name: "filename", value: url.lastPathComponent)])
        let response = try await owner.api.upload(uploadPath, data: bytes, headers: headers)
        let result = try await waitForCommand(device, response: response)
        let savedName = result["filename"] as? String ?? url.lastPathComponent
        do { try await listFiles(device, root: root, path: path) }
        catch { self.error = "Файл сохранён, но список папки не обновлён: " + error.localizedDescription }
        status = "Файл «\(savedName)» сохранён на ПК"
    }
    func getClipboard(_ device: NativeDevice) async throws {
        clipboard = ""
        let result = try await command(device, name: "clipboard_get")
        if result["sealed"] as? Bool == true {
            let blob = try NativeWorkspaceCrypto.bytes(result["blob"], limit: 256 * 1024)
            let plain = try NativeWorkspaceCrypto.open(blob, origin: owner.api.origin, peer: try await peer(device), purpose: "clipboard")
            guard plain.count <= 256 * 1024, let text = String(data: plain, encoding: .utf8) else { throw OwnerAPIError.invalidResponse }
            clipboard = text
        } else { clipboard = result["text"] as? String ?? "" }
    }
    func setClipboard(_ device: NativeDevice, text: String) async throws {
        guard text.unicodeScalars.count <= 64 * 1024 else { throw OwnerAPIError(status: 413, message: "Текст превышает 64 КБ.") }
        var payload: [String: Any] = ["text": text]
        if let key = try await optionalPeer(device) {
            guard let secret = workspacePrivateKey() else { throw NativeWorkspaceCrypto.missingKey }
            let blob = try NativeWorkspaceCrypto.seal(Data(text.utf8), privateKey: secret, peer: key, purpose: "clipboard")
            payload = ["sealed": true, "blob": NativeWorkspaceCrypto.encoded(blob)]
        }
        _ = try await command(device, name: "clipboard_set", payload: payload)
    }
}
