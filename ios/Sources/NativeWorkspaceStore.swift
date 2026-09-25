import Foundation
import Combine

struct NativeSiteLink: Identifiable {
    let id = UUID()
    var label = ""
    var url = ""
}

struct NativeSiteProfile {
    var name = ""
    var title = ""
    var bio = ""
    var username = ""
    var telegramURL = ""
    var avatarURL = ""
    var quote = ""
    var stack = ""
    var links: [NativeSiteLink] = []

    init(_ value: [String: Any] = [:]) {
        name = value["name"] as? String ?? ""
        title = value["title"] as? String ?? ""
        bio = value["bio"] as? String ?? ""
        username = value["username"] as? String ?? ""
        telegramURL = value["telegram_url"] as? String ?? ""
        avatarURL = value["avatar_url"] as? String ?? ""
        quote = value["quote"] as? String ?? ""
        stack = (value["stack"] as? [String] ?? []).joined(separator: ", ")
        links = (value["links"] as? [[String: Any]] ?? []).map {
            NativeSiteLink(label: $0["label"] as? String ?? "", url: $0["url"] as? String ?? "")
        }
    }

    var payload: [String: Any] {
        ["name": name, "title": title, "bio": bio, "username": username,
         "telegram_url": telegramURL, "avatar_url": avatarURL, "quote": quote,
         "stack": stack.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty },
         "links": links.map { ["label": $0.label, "url": $0.url] }]
    }
}

struct NativeSiteProject: Identifiable {
    var id = ""
    var title = ""
    var subtitle = ""
    var description = ""
    var url = ""
    var status = "dev"
    var yearFrom = Calendar.current.component(.year, from: Date())
    var yearTo = Calendar.current.component(.year, from: Date())
    var tags = ""
    var featured = false
    var coverType = "image"
    var coverSource = ""

    init(_ value: [String: Any] = [:]) {
        id = value["id"] as? String ?? ""
        title = value["title"] as? String ?? ""
        subtitle = value["subtitle"] as? String ?? ""
        description = value["description"] as? String ?? ""
        url = value["url"] as? String ?? ""
        status = value["status"] as? String ?? "dev"
        let years = value["years"] as? [String: Any] ?? [:]
        yearFrom = years["from"] as? Int ?? yearFrom
        yearTo = years["to"] as? Int ?? yearTo
        tags = (value["tags"] as? [String] ?? []).joined(separator: ", ")
        featured = value["featured"] as? Bool ?? false
        let cover = value["cover"] as? [String: Any] ?? [:]
        coverType = cover["type"] as? String ?? "image"
        coverSource = cover["src"] as? String ?? ""
    }

    var payload: [String: Any] {
        ["id": id, "title": title, "subtitle": subtitle, "description": description, "url": url,
         "status": status, "year_from": yearFrom, "year_to": yearTo, "featured": featured,
         "tags": tags.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty },
         "cover_type": coverType, "cover_src": coverSource]
    }
}

struct NativeNotification: Identifiable {
    let id: Int
    let title: String
    let message: String
    let createdAt: String
    var unread: Bool
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? Int else { return nil }
        self.id = id
        title = value["title"] as? String ?? "XASS"
        message = value["message"] as? String ?? ""
        createdAt = value["created_at"] as? String ?? ""
        unread = value["read_at"] is NSNull || value["read_at"] == nil
    }
}

/// Native screens use the same bounded, authenticated transport as music.
/// Drafts stay in individual forms; refreshes cannot overwrite unsaved edits.
@MainActor final class NativeWorkspaceStore: ObservableObject {
    let api: OwnerService
    @Published private(set) var dashboard: [String: Any] = [:]
    @Published private(set) var site: [String: Any] = [:]
    @Published private(set) var diagnostics: [String: Any] = [:]
    @Published private(set) var weather: [String: Any] = [:]
    @Published private(set) var notifications: [NativeNotification] = []
    @Published private(set) var unreadCount = 0
    @Published private(set) var loading = false
    @Published private(set) var saving = false
    @Published var error: String?
    @Published var notice: String?
    @Published private(set) var refreshedAt: Date?
    private var pendingLoads = Set<String>()

    init(api: OwnerService) { self.api = api }
    var profile: NativeSiteProfile { NativeSiteProfile(site["profile"] as? [String: Any] ?? [:]) }
    var projects: [NativeSiteProject] { (site["projects"] as? [[String: Any]] ?? []).map(NativeSiteProject.init) }
    var canEdit: Bool { site["can_edit"] as? Bool ?? false }
    var status: [String: Any] { dashboard["status"] as? [String: Any] ?? [:] }
    var settings: [String: Any] { dashboard["settings"] as? [String: Any] ?? [:] }

    func load(_ section: String) async {
        guard pendingLoads.insert(section).inserted else { return }
        loading = true
        defer { pendingLoads.remove(section); loading = !pendingLoads.isEmpty }
        do {
            let paths = ["home": "/api/mini/bootstrap", "site": "/api/mini/site", "diagnostics": "/api/mini/diagnostics",
                         "notifications": "/api/mini/notifications?limit=100", "weather": "/api/mini/weather"]
            guard let path = paths[section] else { return }
            let response = try await api.request(path, method: "GET", body: nil)
            try Task.checkCancellation()
            switch section {
            case "home": dashboard = response; unreadCount = response["notifications_unread"] as? Int ?? 0; refreshedAt = Date()
            case "site": site = response
            case "diagnostics": diagnostics = response
            case "weather": weather = response
            case "notifications":
                notifications = (response["items"] as? [[String: Any]] ?? []).compactMap(NativeNotification.init)
                unreadCount = response["unread_count"] as? Int ?? 0
            default: break
            }
            error = nil
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }

    @discardableResult func save(_ path: String, method: String = "POST", body: [String: Any]? = nil) async -> [String: Any]? {
        guard !saving else { return nil }
        saving = true; error = nil; notice = nil
        defer { saving = false }
        do {
            let response = try await api.request(path, method: method, body: body)
            notice = "Сохранено"
            return response
        } catch { self.error = error.localizedDescription; return nil }
    }

    func saveProfile(_ profile: NativeSiteProfile) async -> Bool {
        guard let response = await save("/api/mini/site/profile", body: profile.payload) else { return false }
        site["profile"] = response["profile"]
        return true
    }
    func saveProject(_ project: NativeSiteProject) async -> Bool {
        guard let response = await save("/api/mini/site/projects", body: project.payload) else { return false }
        site["projects"] = response["projects"]
        return true
    }
    func deleteProject(_ project: NativeSiteProject) async {
        guard await save("/api/mini/site/projects/" + OwnerAPI.pathComponent(project.id), method: "DELETE") != nil else { return }
        await load("site")
    }
    func setting(_ key: String, value: Any) async {
        guard let response = await save("/api/mini/setting", body: ["key": key, "value": value]) else { return }
        dashboard["settings"] = response["settings"]
        dashboard["status"] = response["status"]
    }
    func read(_ notification: NativeNotification) async {
        guard await save("/api/mini/notifications/\(notification.id)/read") != nil else { return }
        await load("notifications")
    }
    func readAll() async {
        guard await save("/api/mini/notifications/read-all") != nil else { return }
        await load("notifications")
    }
}
