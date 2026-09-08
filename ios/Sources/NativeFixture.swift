#if DEBUG && targetEnvironment(simulator)
import Foundation

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
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        // Fail closed: fixture gestures never reach a network or execute an action.
        if method != "GET" { throw OwnerAPIError(status: 403, message: "Предпросмотр: действия с сервером и устройствами отключены.") }
        if path.contains("bootstrap") { return ["ok": true, "sources": Self.deviceData] }
        if path.contains("library") { return ["ok": true, "tracks": Self.trackData, "playlists": [], "has_more": false] }
        if path.contains("storage") { return ["ok": true, "targets": [], "copies": [], "jobs": [], "server_available": true] }
        if path.contains("players") { return ["ok": true, "players": []] }
        return ["ok": true, "session": [:], "commands": []]
    }
}

extension NativeStore {
    func installFixture() {
        tracks = NativeFixture.trackData.compactMap(LibraryTrack.init)
        playlists = [["id": 1, "name": "Вечер", "track_ids": [1, 3, 5]]].compactMap(LibraryPlaylist.init)
        devices = NativeFixture.deviceData.compactMap(NativeDevice.init)
        outputs = [["id": "default", "name": "По умолчанию"], ["id": "fixture-speakers", "name": "Динамики (Realtek Audio)"]].compactMap(PlayerOutput.init)
        authorized = true; currentID = 1; duration = 224; position = 84; playbackState = "paused"
    }
}
#endif
