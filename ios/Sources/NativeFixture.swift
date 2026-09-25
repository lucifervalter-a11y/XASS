#if DEBUG && targetEnvironment(simulator)
import Foundation
import UIKit

/// Explicit simulator-only data. No transport, media, authentication or device commands.
@MainActor final class NativeFixture: OwnerService {
    static var enabled: Bool { ProcessInfo.processInfo.arguments.contains("--native-ui-fixture") }
    static var screen: String {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "--native-ui-screen"), args.indices.contains(index + 1) else { return "library" }
        return args[index + 1]
    }
    let origin: ServerOrigin
    init(origin: ServerOrigin) { self.origin = origin }
    func artwork(trackID: Int) async throws -> Data? {
        guard trackID == 1 else { return nil }
        // A labelled generated test JPEG proves the exact production image view.
        // This graphic exists only in Debug Simulator, never a production album.
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: 320, height: 320))
        return renderer.image { context in
            UIColor(red: 0.06, green: 0.13, blue: 0.23, alpha: 1).setFill(); context.fill(CGRect(x: 0, y: 0, width: 320, height: 320))
            for index in 0..<9 {
                let path = UIBezierPath(); path.lineWidth = 3
                path.move(to: CGPoint(x: -10, y: CGFloat(100 + index * 13))); path.addCurve(to: CGPoint(x: 330, y: CGFloat(160 + index * 10)), controlPoint1: CGPoint(x: 90, y: CGFloat(-40 + index * 18)), controlPoint2: CGPoint(x: 180, y: CGFloat(370 - index * 10)))
                UIColor(red: 0.23, green: 0.51 + CGFloat(index) * 0.025, blue: 0.92, alpha: 0.65).setStroke(); path.stroke()
            }
            ("XASS · TEST ART" as NSString).draw(at: CGPoint(x: 20, y: 285), withAttributes: [.font: UIFont.systemFont(ofSize: 12, weight: .medium), .foregroundColor: UIColor.white])
        }.jpegData(compressionQuality: 0.88)
    }
    static let trackData: [[String: Any]] = [
        ["id": 1, "title": "Тихий город", "artist": "Тестовая библиотека", "duration": 224, "favorite": true],
        ["id": 2, "title": "Северный свет", "artist": "Тестовая библиотека", "duration": 196],
        ["id": 3, "title": "После дождя", "artist": "Тестовая библиотека", "duration": 243, "favorite": true],
        ["id": 4, "title": "На другой стороне", "artist": "Тестовая библиотека", "duration": 218],
        ["id": 5, "title": "Дорога домой", "artist": "Тестовая библиотека", "duration": 207],
        ["id": 6, "title": "Последний поезд", "artist": "Тестовая библиотека", "duration": 256]
    ]
    static let deviceData: [[String: Any]] = [
        ["id": 1, "source_type": "PC_AGENT", "source_name": "Студия", "is_online": true, "agent_version": "0.17.0"],
        ["id": 2, "source_type": "PC_AGENT", "source_name": "Ноутбук", "is_online": false, "agent_version": "0.17.0"]
    ]
    static let playlistData: [[String: Any]] = [
        ["id": 1, "name": "Вечер", "track_ids": [1, 3, 5]]
    ]
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        // Fail closed: fixture gestures never reach a network or execute an action.
        if method != "GET" { throw OwnerAPIError(status: 403, message: "Предпросмотр: действия с сервером и устройствами отключены.") }
        let components = URLComponents(string: path)
        let endpoint = components?.path ?? path
        if endpoint == "/api/mini/bootstrap" {
            return ["ok": true, "sources": Self.deviceData, "app_version": "0.18.0",
                    "user": ["first_name": "Артём", "is_owner": true],
                    "status": ["name": "Тестовый XASS"],
                    "metrics": ["cpu_percent": 12, "ram_used_percent": 38],
                    "health_summary": ["status": "online", "title": "Всё работает"],
                    "system_status": ["backend": ["available": true, "status": "online"], "database": ["available": true, "status": "online"], "telegram_bot": ["available": true, "status": "online"], "public_site": ["available": true, "status": "online"]],
                    "notifications_unread": 0]
        }
        if endpoint == "/api/mini/music/library" {
            let queryItems = components?.queryItems ?? []
            func value(_ key: String) -> String? { queryItems.first { $0.name == key }?.value }
            let query = (value("q") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let filtered = Self.trackData.filter { row in
                let matchesQuery = query.isEmpty || [row["title"], row["artist"]].compactMap { $0 as? String }.contains { $0.localizedCaseInsensitiveContains(query) }
                return matchesQuery && (value("favorite") != "true" || row["favorite"] as? Bool == true)
            }
            let offset = max(0, Int(value("offset") ?? "0") ?? 0)
            let limit = max(1, min(200, Int(value("limit") ?? "50") ?? 50))
            let tracks = Array(filtered.dropFirst(offset).prefix(limit))
            let nextOffset = offset + tracks.count
            var result: [String: Any] = ["ok": true, "tracks": tracks, "playlists": Self.playlistData, "has_more": nextOffset < filtered.count]
            if nextOffset < filtered.count { result["next_offset"] = nextOffset }
            return result
        }
        if endpoint == "/api/mini/site" {
            return ["ok": true, "profile": ["name": "Артём", "title": "Мой XASS", "bio": "Музыка, устройства и личный сайт в одном приложении."],
                    "projects": [], "site_config": ["accent_color": "#5C8DFF", "widgets": []], "can_edit": true]
        }
        if endpoint == "/api/mini/weather" {
            return ["ok": true, "weather": ["location_name": "Москва", "temperature": "18 °C", "feels_like": "17 °C", "wind_speed": "3 м/с", "humidity": "62%", "weather_text": "Переменная облачность", "updated_time": "12:00"]]
        }
        if endpoint == "/api/mini/notifications" { return ["ok": true, "items": [], "unread_count": 0] }
        if endpoint == "/api/mini/diagnostics" {
            return ["ok": true, "database": ["available": true], "telegram": ["configured": true], "storage": ["disk_free_gb": 42], "configuration": ["ok": true, "issues": []]]
        }
        if path.contains("storage") { return ["ok": true, "targets": [], "copies": [], "jobs": [], "server_available": true] }
        if path.contains("players") { return ["ok": true, "players": []] }
        return ["ok": true, "session": [:], "commands": []]
    }
}

extension NativeStore {
    func installFixture() {
        tracks = NativeFixture.trackData.compactMap(LibraryTrack.init)
        playlists = NativeFixture.playlistData.compactMap(LibraryPlaylist.init)
        devices = NativeFixture.deviceData.compactMap(NativeDevice.init)
        outputs = [["id": "default", "name": "По умолчанию"], ["id": "fixture-speakers", "name": "Динамики (Realtek Audio)"]].compactMap(PlayerOutput.init)
        authorized = true; currentID = 1; duration = 224; position = 84; playbackState = "paused"
    }
}
#endif
