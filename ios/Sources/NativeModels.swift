import Foundation

struct LibraryTrack: Identifiable, Equatable {
    let id: Int
    var title: String
    var artist: String
    var album: String
    var duration: Double
    var favorite: Bool
    var mime: String
    var filename: String
    var pcSupported: Bool { mime != "audio/mp4" && mime != "audio/x-m4a" }
    var nativeQueueItem: [String: Any] { ["trackId": id, "title": title, "artist": artist] }
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? Int, id > 0 else { return nil }
        self.id = id; title = value["title"] as? String ?? "Без названия"; artist = value["artist"] as? String ?? ""
        album = value["album"] as? String ?? ""; duration = NativeValue.number(value["duration"])
        favorite = value["favorite"] as? Bool ?? false; mime = value["mime"] as? String ?? ""
        filename = value["filename"] as? String ?? title
    }
    init(_ download: DownloadedTrack) {
        id = download.id; title = download.title; artist = download.artist; album = ""; duration = download.duration
        favorite = false; mime = ""; filename = download.title
    }
}

struct LibraryPlaylist: Identifiable, Equatable {
    let id: Int
    var name: String
    var trackIDs: [Int]
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? Int, id > 0 else { return nil }
        self.id = id; name = value["name"] as? String ?? "Плейлист"; trackIDs = value["track_ids"] as? [Int] ?? []
    }
}

struct NativeDevice: Identifiable, Equatable {
    let id: Int
    let name: String
    var online: Bool
    let version: String
    let lastSeen: String?
    let cpu: Double?
    let memory: Double?
    let attention: [String]
    init?(_ value: [String: Any]) {
        guard value["source_type"] as? String == "PC_AGENT", let id = value["id"] as? Int,
              let name = value["source_name"] as? String, !name.isEmpty else { return nil }
        self.id = id; self.name = name; online = value["is_online"] as? Bool ?? false
        version = value["agent_version"] as? String ?? ""; lastSeen = value["last_seen_at"] as? String
        let payload = value["last_payload"] as? [String: Any] ?? [:]
        let metrics = payload["metrics"] as? [String: Any] ?? payload
        cpu = (metrics["cpu_percent"] as? NSNumber)?.doubleValue; memory = (metrics["ram_used_percent"] as? NSNumber)?.doubleValue
        attention = value["attention_reasons"] as? [String] ?? []
    }
}

struct PlayerOutput: Identifiable, Equatable {
    let id: String
    let name: String
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, let name = value["name"] as? String else { return nil }
        self.id = id; self.name = name
    }
}

struct RemotePlayer: Identifiable {
    let id: String
    let online: Bool
    let available: Bool
    let trackID: Int?
    let state: String
    let position: Double
    let duration: Double
    let volume: Double
    let outputID: String
    let error: String?
    init?(_ value: [String: Any]) {
        guard let name = value["source_name"] as? String else { return nil }
        id = name; online = value["online"] as? Bool ?? false; available = value["available"] as? Bool ?? false
        let info = value["music_player"] as? [String: Any] ?? [:]
        trackID = info["track_id"] as? Int; state = info["state"] as? String ?? "stopped"
        position = NativeValue.number(info["position_sec"]); duration = NativeValue.number(info["duration_sec"])
        volume = NativeValue.number(info["volume"], fallback: 70); outputID = info["output_id"] as? String ?? "default"
        error = (info["error"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }
}

enum NativeValue {
    static func identifier(_ value: Any?) -> String? {
        if let text = value as? String, !text.isEmpty, text.count <= 160 { return text }
        if let id = value as? Int, id > 0 { return String(id) }; return nil
    }
    static func number(_ value: Any?, fallback: Double = 0) -> Double {
        let number = (value as? NSNumber)?.doubleValue ?? fallback
        return number.isFinite ? max(0, number) : fallback
    }
    static func time(_ value: Double) -> String {
        let safe = value.isFinite ? min(max(0, value), 86400) : 0, total = Int(safe)
        return String(format: "%d:%02d", total / 60, total % 60)
    }
    static func queue(_ tracks: [LibraryTrack], currentID: Int) -> [[String: Any]] {
        let current = tracks.firstIndex(where: { $0.id == currentID }) ?? 0
        let start = max(0, min(current - 100, tracks.count - 200))
        return tracks.dropFirst(start).prefix(200).map(\.nativeQueueItem)
    }
}
