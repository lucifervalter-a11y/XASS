import Foundation
import Combine
import UIKit

@MainActor final class NativeStore: ObservableObject {
    let api: OwnerService
    let audio: AudioController
    let authorization: NativeActionAuthorization
    let sessionKey: String
    let clientID: String
    @Published var tracks: [LibraryTrack] = []
    @Published var playlists: [LibraryPlaylist] = []
    @Published var devices: [NativeDevice] = []
    @Published var players: [RemotePlayer] = []
    @Published var outputs: [PlayerOutput] = []
    @Published var authorized = false
    @Published var loading = false
    @Published var busy = false
    @Published var error: String?
    @Published var notice: String?
    @Published var showLogin = false
    @Published var showEnrollment = false
    @Published var showPlayer = false
    @Published var currentID: Int?
    @Published var selectedDevice = "local"
    @Published private(set) var canonicalClientID = ""
    private var canonicalSessionKey = ""
    private var canonicalDetail: String?
    @Published var outputID = "default"
    @Published var playbackState = "stopped"
    @Published var position: Double = 0
    @Published var duration: Double = 0
    @Published var volume: Double = 70
    @Published var shareSite = false
    @Published var shareSaving = false
    @Published var shuffle = false
    @Published var queueSaving = false
    @Published var repeatMode = "off"
    @Published var uploadName: String?
    @Published var uploadProgress: Double = 0
    @Published private(set) var libraryHasMore = false
    @Published private(set) var libraryLoadingMore = false
    @Published private(set) var transferStatus: String?
    @Published private(set) var transferProgress: Double = 0
    private var libraryNextOffset: Int?
    private var libraryQuery = ""
    private var libraryFavorite = false
    private let libraryPageSize = 50
    private var transferCancelled = false
    private var activeTransferID: String?
    /// User explicitly chose a route via AirPlay-like picker. Until then, play on this iPhone.
    private var userPickedRoute = false
    @Published var showRoutePicker = false
    var confirmationActivity: ((Bool) -> Void)?
    var canSendActions: () -> Bool = { UIApplication.shared.applicationState == .active }
    private(set) var queue: [LibraryTrack] = []
    private var baseQueue: [LibraryTrack] = []
    private var ownsSession = false
    private var offlinePlayback = false
    private var suppressReports = false
    private var reportTask: Task<Void, Never>?
    private var pendingReport: [String: Any]?
    private var pollTask: Task<Void, Never>?
    private var sessionWrite: Task<[String: Any], Error>?
    private var sessionWriteID = UUID()
    private var generation = UUID()
    private var inboxBusy = false
    private var transferPending = false
    private var outputRequest = UUID()
    private var acknowledgedCommands: [String: [String: Any]] = [:]
    private let imageCache = NSCache<NSNumber, UIImage>()
    private var missingArtwork = Set<Int>()
    @Published private(set) var artworkBytes = 0
    private var maxUpload = 256 * 1024 * 1024

    init(api: OwnerService, audio: AudioController) {
        self.api = api; self.audio = audio; authorization = NativeActionAuthorization(api: api)
        sessionKey = Self.persistedID("native-player-", origin: api.origin)
        clientID = Self.persistedID("native-client-", origin: api.origin)
        imageCache.countLimit = 80; imageCache.totalCostLimit = 16 * 1024 * 1024
        audio.configure(api.origin)
        audio.emit = { [weak self] event in self?.audioEvent(event) }
        audio.reportSession = { [weak self] snapshot in self?.queueReport(snapshot) }
        audio.canResumePlayback = { [weak self] in
            guard let self = self else { return false }
            return !self.transferPending && (self.ownsSession || self.offlinePlayback)
        }
        audio.requestTicket = { [weak self] id, done in
            Task { @MainActor in
                guard let self = self else { done(.failure(OwnerAPIError.signedOut)); return }
                do { done(.success(try await self.ticket(id))) } catch { done(.failure(error)) }
            }
        }
    }
    private static func persistedID(_ prefix: String, origin: ServerOrigin) -> String {
        let name = prefix + origin.namespace
        if let data = SecureStore.load(name), let value = String(data: data, encoding: .utf8), value.count >= 16, value.count <= 64 { return value }
        let value = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        try? SecureStore.save(Data(value.utf8), name: name); return value
    }
    var currentTrack: LibraryTrack? {
        tracks.first(where: { $0.id == currentID }) ?? audio.downloads.first(where: { $0.id == currentID }).map(LibraryTrack.init) ?? audio.cachedTracks.first(where: { $0.id == currentID }).map { LibraryTrack($0.track) }
    }
    var otherLocal: Bool { selectedDevice == "local" && !canonicalSessionKey.isEmpty && (canonicalSessionKey != sessionKey || (!canonicalClientID.isEmpty && canonicalClientID != clientID)) }
    var deviceLabel: String { selectedDevice == "local" ? (otherLocal ? "Другое устройство" : "Этот iPhone") : String(selectedDevice.dropFirst(6)) }
    var playing: Bool { playbackState == "playing" }
    var canEditQueue: Bool { !queueSaving && !busy && (authorized || offlinePlayback) }
    var enrolled: Bool { authorization.identity.enrolled }
    func rows(filter: String, query: String, playlist: LibraryPlaylist? = nil) -> [LibraryTrack] {
        var rows = playlist.map { p in p.trackIDs.compactMap { id in tracks.first { $0.id == id } } } ?? tracks
        if filter == "favorites" { rows = rows.filter(\.favorite) }
        let search = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !search.isEmpty { rows = rows.filter { ($0.title + " " + $0.artist + " " + $0.album).localizedCaseInsensitiveContains(search) } }
        return rows
    }
    func run(_ action: @escaping () async throws -> Void) {
        Task { do { try await action() } catch is CancellationError {} catch { self.handle(error) } }
    }
    func handle(_ failure: Error) {
        if let apiError = failure as? OwnerAPIError {
            if apiError.status == 401 { authorized = false; ownsSession = false; showLogin = true }
            if apiError.status == 428 { showEnrollment = true }
        }
        error = failure.localizedDescription
    }
    func refresh() async {
        guard !loading else { return }; loading = true; defer { loading = false }
        do {
            let bootstrap = try await api.request("/api/mini/bootstrap", method: "GET", body: nil)
            devices = (bootstrap["sources"] as? [[String: Any]] ?? []).compactMap(NativeDevice.init)
            libraryQuery = ""; libraryFavorite = false
            let library = try await api.request("/api/mini/music/library?limit=\(libraryPageSize)&offset=0", method: "GET", body: nil)
            var seen = Set<Int>()
            tracks = (library["tracks"] as? [[String: Any]] ?? []).compactMap(LibraryTrack.init).filter { seen.insert($0.id).inserted }
            playlists = (library["playlists"] as? [[String: Any]] ?? []).compactMap(LibraryPlaylist.init)
            maxUpload = library["max_upload_bytes"] as? Int ?? maxUpload
            libraryHasMore = library["has_more"] as? Bool == true
            libraryNextOffset = library["next_offset"] as? Int
            authorized = true; showLogin = false; error = nil
            try await refreshSession()
        } catch { handle(error) }
    }

    func loadMoreTracks() async {
        guard libraryHasMore, !libraryLoadingMore, !loading, let offset = libraryNextOffset else { return }
        libraryLoadingMore = true; defer { libraryLoadingMore = false }
        do {
            var path = "/api/mini/music/library?limit=\(libraryPageSize)&offset=\(offset)"
            let q = libraryQuery.trimmingCharacters(in: .whitespacesAndNewlines)
            if !q.isEmpty { path += "&q=\(q.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? q)" }
            if libraryFavorite { path += "&favorite=true" }
            let page = try await api.request(path, method: "GET", body: nil)
            let incoming = (page["tracks"] as? [[String: Any]] ?? []).compactMap(LibraryTrack.init)
            var seen = Set(tracks.map(\.id))
            tracks.append(contentsOf: incoming.filter { seen.insert($0.id).inserted })
            libraryHasMore = page["has_more"] as? Bool == true
            libraryNextOffset = page["next_offset"] as? Int
            if let lists = page["playlists"] as? [[String: Any]] { playlists = lists.compactMap(LibraryPlaylist.init) }
        } catch { handle(error) }
    }
    func searchLibrary(query: String, favorite: Bool = false) async {
        libraryQuery = query; libraryFavorite = favorite
        guard !loading else { return }; loading = true; defer { loading = false }
        do {
            var path = "/api/mini/music/library?limit=\(libraryPageSize)&offset=0"
            let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
            if !q.isEmpty { path += "&q=\(q.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? q)" }
            if favorite { path += "&favorite=true" }
            let library = try await api.request(path, method: "GET", body: nil)
            var seen = Set<Int>()
            tracks = (library["tracks"] as? [[String: Any]] ?? []).compactMap(LibraryTrack.init).filter { seen.insert($0.id).inserted }
            if let lists = library["playlists"] as? [[String: Any]] { playlists = lists.compactMap(LibraryPlaylist.init) }
            libraryHasMore = library["has_more"] as? Bool == true
            libraryNextOffset = library["next_offset"] as? Int
            error = nil
        } catch { handle(error) }
    }
    
    func openRoutePicker() {
        // Mutually exclusive: close player sheet while choosing route.
        showPlayer = false
        showLogin = false
        showEnrollment = false
        showRoutePicker = true
    }

    func pickRoute(device: String, output: String? = nil) async throws {
        userPickedRoute = true
        selectedDevice = device
        if let output = output { outputID = output }
        if currentID != nil {
            // Keep the same AirPlay sheet open and show progress inside it.
            showRoutePicker = true
            showPlayer = false
            do {
                try await transfer(to: device, trackID: currentID, startPosition: position, output: output)
                showRoutePicker = false
            } catch is CancellationError {
                showRoutePicker = true
                throw CancellationError()
            } catch {
                showRoutePicker = true
                throw error
            }
        } else {
            showRoutePicker = false
            notice = device == "local" ? "Будет играть на этом iPhone" : "Устройство выбрано"
        }
    }


    /// Background/locked iPhone still owns the audible source: ACK stop from session poll.
    private func acknowledgePendingHandoff(from session: [String: Any]) async {
        guard let transferID = session["active_transfer_id"] as? String,
              (session["active_transfer_status"] as? String) == "waiting",
              let sourceKey = session["active_transfer_source_key"] as? String,
              sourceKey == sessionKey else { return }
        suppressReports = true; audio.pause(); suppressReports = false
        let position = audio.exactPosition()
        ownsSession = false
        _ = try? await api.request("/api/mini/music/transfers/" + OwnerAPI.pathComponent(transferID) + "/ack", method: "POST", body: ["session_key": sessionKey, "position": position])
    }

func cancelTransfer() {
        // Only signal cancel. Clearing busy/transferPending here races a second POST / inbox.
        // transfer()/remoteControl() clear locks in defer after server cancel.
        let transferID = activeTransferID
        let wasRemote = transferStatus?.contains("Команда") == true || transferStatus?.contains("подтвержден") == true || transferStatus?.contains("жду ответ") == true
        transferCancelled = true
        generation = UUID()
        transferStatus = "Отменяю на сервере…"
        notice = "Переключение отменено"
        guard let transferID, !transferID.isEmpty else { return }
        let path = wasRemote
            ? "/api/mini/music/session/commands/" + OwnerAPI.pathComponent(transferID) + "/cancel"
            : "/api/mini/music/transfers/" + OwnerAPI.pathComponent(transferID) + "/cancel"
        Task { try? await api.request(path, method: "POST", body: nil) }
    }

    func foreground(_ active: Bool) {
        pollTask?.cancel(); pollTask = nil
        guard active else { return }
        pollTask = Task { [weak self] in
            guard let self = self else { return }
            await self.refresh()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(5)); try Task.checkCancellation(); if self.authorized { await self.processInbox(); if !self.busy { try await self.refreshSession() } } }
                catch is CancellationError { break }
                catch { self.handle(error) }
            }
        }
    }
    func disconnect() {
        generation = UUID(); foreground(false); ownsSession = false; pendingReport = nil; suppressReports = true
        reportTask?.cancel(); reportTask = nil; audio.disconnect(); imageCache.removeAllObjects()
        (api as? OwnerAPI)?.invalidate()
    }
    private func refreshSession() async throws {
        let response = try await api.request("/api/mini/music/session", method: "GET", body: nil)
        let session = response["session"] as? [String: Any] ?? [:]
        let serverKey = session["session_key"] as? String
        if ownsSession && serverKey != nil && serverKey != sessionKey {
            generation = UUID(); ownsSession = false; suppressReports = true; audio.pause(); suppressReports = false; pendingReport = nil
        }
        if !offlinePlayback && (!ownsSession || selectedDevice != "local") { applySession(session) }
        await acknowledgePendingHandoff(from: session)
        if ownsSession && !queueSaving, let ids = session["queue"] as? [Int], !ids.isEmpty {
            let mode = session["repeat_mode"] as? String ?? repeatMode
            if ids != queue.map(\.id) || mode != repeatMode {
                queue = ids.compactMap { id in tracks.first { $0.id == id } }; baseQueue = queue; repeatMode = mode
                applyNativeQueue()
            }
        }
        let result = try await api.request("/api/mini/music/players", method: "GET", body: nil)
        players = (result["players"] as? [[String: Any]] ?? []).compactMap(RemotePlayer.init)
        for index in devices.indices { if let player = players.first(where: { $0.id == devices[index].name }) { devices[index].online = player.online } }
        // /session reconciles queue-command ACKs with heartbeat freshness.
        // /players supplies discovery/capabilities only: its raw heartbeat can
        // still describe the previous track during server-owned auto-advance.
    }
    private func applySession(_ value: [String: Any]) {
        currentID = value["track_id"] as? Int
        canonicalClientID = value["client_id"] as? String ?? ""; canonicalSessionKey = value["session_key"] as? String ?? ""
        if userPickedRoute || transferPending { selectedDevice = value["device"] as? String ?? selectedDevice } else { selectedDevice = "local" }
        playbackState = value["state"] as? String ?? "stopped"
        let detail = value["detail"] as? String
        if let detail = detail { error = detail }
        else if error == canonicalDetail { error = nil }
        canonicalDetail = detail
        position = NativeValue.number(value["position"]); duration = currentTrack?.duration ?? 0
        outputID = value["output_id"] as? String ?? "default"; volume = NativeValue.number(value["volume"], fallback: volume)
        if !shareSaving { shareSite = value["share_site"] as? Bool ?? shareSite }
        if !queueSaving, let ids = value["queue"] as? [Int], !ids.isEmpty { queue = ids.compactMap { id in tracks.first { $0.id == id } }; if baseQueue.isEmpty { baseQueue = queue } }
        if let mode = value["repeat_mode"] as? String, ["off", "one", "all"].contains(mode) { repeatMode = mode }
    }
    private func audioEvent(_ value: [String: Any]) {
        if value["action"] != nil { objectWillChange.send(); return }
        guard selectedDevice == "local", ownsSession || offlinePlayback || transferPending else { return }
        if let id = value["trackId"] as? Int, id > 0 { currentID = id }
        playbackState = value["state"] as? String ?? playbackState
        position = NativeValue.number(value["position"]); duration = NativeValue.number(value["duration"], fallback: duration)
        if let message = value["error"] as? String { error = message }
    }
    func ticket(_ trackID: Int, purpose: String = "listen") async throws -> String {
        let result = try await api.request("/api/mini/music/tracks/\(trackID)/ticket", method: "POST", body: ["purpose": purpose])
        guard let path = result["path"] as? String else { throw OwnerAPIError.invalidResponse }
        _ = try api.origin.mediaURL(path, trackID: trackID)
        var parts = URLComponents(url: api.origin.url.appendingPathComponent("proxy.php"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [.init(name: "_binary", value: "1"), .init(name: "_media", value: "1"), .init(name: "_p", value: path)]
        return parts.url!.absoluteString
    }
    func artwork(_ id: Int) async -> UIImage? {
        if let image = imageCache.object(forKey: NSNumber(value: id)) { return image }
        if missingArtwork.contains(id) { return nil }
        do {
            guard let data = try await api.artwork(trackID: id), let image = UIImage(data: data) else { missingArtwork.insert(id); return nil }
            imageCache.setObject(image, forKey: NSNumber(value: id), cost: data.count); return image
        } catch { return nil }
    }
    func clearArtworkCache() { imageCache.removeAllObjects(); missingArtwork.removeAll(); artworkBytes = 0; notice = "Кэш обложек очищен. Скачанная музыка не затронута." }
    func playOffline(_ track: DownloadedTrack) {
        ownsSession = false; offlinePlayback = true; suppressReports = true; selectedDevice = "local"; currentID = track.id; canonicalSessionKey = ""; canonicalClientID = clientID
        audio.playOffline(track); suppressReports = false; showPlayer = true
    }
    func play(_ track: LibraryTrack, rows: [LibraryTrack]? = nil) async throws {
        if let rows = rows { baseQueue = rows; queue = shuffle ? rows.shuffled() : rows }
        if queue.isEmpty { baseQueue = tracks; queue = shuffle ? tracks.shuffled() : tracks }
        if !queue.contains(where: { $0.id == track.id }) { queue.insert(track, at: 0) }
        // Local play must go straight into AVPlayer — never through /transfers.
        if selectedDevice == "local" || selectedDevice.isEmpty {
            try await playLocal(track, startPosition: 0)
        } else {
            try await transfer(to: selectedDevice, trackID: track.id, startPosition: 0)
        }
    }

    /// Start or resume on this iPhone without a handoff lease.
    private func playLocal(_ track: LibraryTrack, startPosition: Double) async throws {
        guard !busy else { return }; busy = true
        defer { busy = false }
        let nextGeneration = UUID(); generation = nextGeneration
        transferCancelled = false
        offlinePlayback = false
        selectedDevice = "local"
        currentID = track.id
        error = nil
        let url: String
        do {
            if audio.downloads.contains(where: { $0.id == track.id }) || audio.cachedTracks.contains(where: { $0.id == track.id }) {
                url = ""
            } else {
                url = try await ticket(track.id)
            }
        } catch let failure {
            playbackState = "error"
            self.error = (failure as? LocalizedError)?.errorDescription ?? "Не удалось получить поток. Проверьте сеть и войдите снова."
            throw failure
        }
        guard generation == nextGeneration, !transferCancelled else { throw CancellationError() }
        ownsSession = true
        position = startPosition
        audio.handle(try NativeAudioCommand(["action": "play", "trackId": track.id, "title": track.title, "artist": track.artist,
            "url": url, "position": startPosition, "volume": volume, "session": snapshot(), "queue": NativeValue.queue(queue, currentID: track.id), "repeat": repeatMode]))
        _ = try? await writeSession(snapshot(state: "playing", position: startPosition), explicit: true)
        showPlayer = true
    }
    func playAll(_ rows: [LibraryTrack], shuffled: Bool) async throws {
        guard !rows.isEmpty, !busy else { return }
        shuffle = shuffled; baseQueue = rows; queue = shuffled ? rows.shuffled() : rows
        if let track = queue.first { try await play(track) }
    }
    func transfer(to device: String, trackID: Int? = nil, startPosition: Double? = nil, output: String? = nil) async throws {
        guard !busy else { return }; busy = true
        defer { busy = false; transferStatus = nil; transferProgress = 0; activeTransferID = nil }
        let nextGeneration = UUID(); generation = nextGeneration; transferPending = true
        defer { transferPending = false }
        if offlinePlayback { suppressReports = true; audio.pause(); suppressReports = false }
        let chosenID = trackID ?? currentID
        guard let chosenID = chosenID else { selectedDevice = device; outputID = output ?? "default"; return }
        if device != "local", tracks.first(where: { $0.id == chosenID })?.pcSupported == false {
            throw OwnerAPIError(status: 415, message: "Этот формат доступен на iPhone. Для ПК загрузите MP3 или WAV.")
        }
        let ownedSource = selectedDevice == "local" && (ownsSession || canonicalSessionKey == sessionKey)
        let actualPosition = ownedSource && audio.hasPlayableItem ? audio.exactPosition() : position
        if ownedSource {
            suppressReports = true; audio.pause(); suppressReports = false
            _ = try await writeSession(snapshot(state: "paused", position: actualPosition), explicit: true)
            ownsSession = false; pendingReport = nil
        }
        var body: [String: Any] = ["session_key": sessionKey, "client_id": clientID, "device": device,
            "output_id": output ?? (device == selectedDevice ? outputID : "default"), "track_id": chosenID, "volume": Int(volume), "autoplay": true,
            "queue": Array(queue.prefix(2000)).map(\.id), "repeat_mode": repeatMode]
        if let start = startPosition { body["position"] = start }
        else if ownedSource { body["position"] = actualPosition }
        let started = try await api.request("/api/mini/music/transfers", method: "POST", body: body)
        guard let transferID = started["transfer_id"] as? String else { throw OwnerAPIError.invalidResponse }
        activeTransferID = transferID; transferCancelled = false
        transferStatus = "Подключаю устройство…"; transferProgress = 0.08
        let deadline = Date().addingTimeInterval(30)
        let startedAt = Date()
        var response = started
        while response["status"] as? String != "ready" {
            if transferCancelled || generation != nextGeneration {
                let id = activeTransferID ?? transferID
                if !id.isEmpty {
                    try? await api.request("/api/mini/music/transfers/" + OwnerAPI.pathComponent(id) + "/cancel", method: "POST", body: nil)
                }
                activeTransferID = nil
                throw CancellationError()
            }
            if response["status"] as? String == "failed" {
                transferStatus = nil; transferProgress = 0; activeTransferID = nil
                throw OwnerAPIError(status: 409, message: response["detail"] as? String ?? "Источник не подтвердил остановку. Переключение отменено.")
            }
            guard Date() < deadline else {
                let id = activeTransferID ?? transferID
                try? await api.request("/api/mini/music/transfers/" + OwnerAPI.pathComponent(id) + "/cancel", method: "POST", body: nil)
                activeTransferID = nil
                throw OwnerAPIError(status: 408, message: "Устройство не подтвердило переключение. Второй плеер не запущен.")
            }
            let elapsed = Date().timeIntervalSince(startedAt)
            transferProgress = min(0.92, 0.08 + elapsed / 30)
            transferStatus = elapsed < 8 ? "Жду остановку на текущем устройстве…" : (elapsed < 18 ? "Передаю воспроизведение…" : "Почти готово…")
            try await Task.sleep(for: .milliseconds(650)); try Task.checkCancellation()
            response = try await api.request("/api/mini/music/transfers/" + OwnerAPI.pathComponent(transferID), method: "GET", body: nil)
        }
        transferProgress = 1; transferStatus = "Готово"
        guard generation == nextGeneration, !transferCancelled else {
            activeTransferID = nil
            throw CancellationError()
        }
        guard let session = response["session"] as? [String: Any] else {
            activeTransferID = nil
            playbackState = "error"
            error = "Сервер подтвердил переключение без сессии. Повторите воспроизведение."
            // Roll back optimistic UI that looked like success.
            try? await api.request("/api/mini/music/transfers/" + OwnerAPI.pathComponent(transferID) + "/cancel", method: "POST", body: nil)
            throw OwnerAPIError(status: 409, message: error ?? "Нет сессии после переключения")
        }
        offlinePlayback = false; applySession(session); currentID = chosenID; error = nil
        if selectedDevice == "local" {
            guard let track = currentTrack else {
                activeTransferID = nil
                playbackState = "error"
                throw OwnerAPIError.invalidResponse
            }
            let url: String
            do {
                if audio.downloads.contains(where: { $0.id == chosenID }) || audio.cachedTracks.contains(where: { $0.id == chosenID }) { url = "" }
                else { url = try await ticket(chosenID) }
            } catch {
                playbackState = "error"
                _ = try? await writeSession(snapshot(state: "error"), explicit: true)
                activeTransferID = nil
                throw error
            }
            guard generation == nextGeneration, !transferCancelled else {
                // Cancel between ready → ticket → play: do not leave UI "playing" without AVPlayer.
                activeTransferID = nil
                ownsSession = false
                playbackState = "paused"
                suppressReports = true; audio.pause(); suppressReports = false
                throw CancellationError()
            }
            ownsSession = true
            audio.handle(try NativeAudioCommand(["action": "play", "trackId": chosenID, "title": track.title, "artist": track.artist,
                "url": url, "position": position, "volume": volume, "session": snapshot(), "queue": NativeValue.queue(queue, currentID: chosenID), "repeat": repeatMode]))
            activeTransferID = nil
        } else {
            ownsSession = false
            activeTransferID = nil
        }
        // Close route picker only after handoff actually finished.
        showRoutePicker = false
    }
    func toggle() async throws {
        guard currentID != nil, !busy else { return }
        if otherLocal { try await remoteControl(playing ? "pause" : "resume"); return }
        if selectedDevice == "local" {
            if ownsSession || offlinePlayback { playing ? audio.pause() : audio.resume() }
            else { try await transfer(to: "local") }
        } else { _ = try await control(playing ? "pause" : "resume"); try await refreshSession() }
    }
    func seek(_ value: Double) async throws {
        if otherLocal { try await remoteControl("seek", extra: ["position": value]); return }
        if selectedDevice == "local" {
            if !ownsSession && !offlinePlayback { try await transfer(to: "local", startPosition: value) }
            else { audio.seek(value) }
        } else { _ = try await control("seek", extra: ["position_sec": value]); position = value }
    }
    func setVolume(_ value: Double) async throws {
        if otherLocal { try await remoteControl("volume", extra: ["volume": Int(value)]); return }
        let bounded = min(100, max(0, value))
        if selectedDevice == "local" { volume = bounded; audio.handle(try NativeAudioCommand(["action": "volume", "volume": bounded])) }
        else { _ = try await control("volume", extra: ["volume": Int(bounded)]) }
        volume = bounded
    }
    func step(_ direction: Int) async throws {
        if otherLocal { try await remoteControl(direction < 0 ? "previous" : "next"); return }
        if selectedDevice == "local" && (ownsSession || offlinePlayback) { audio.handle(try NativeAudioCommand(["action": direction < 0 ? "previous" : "next"])); return }
        if direction < 0 && position > 3 { try await seek(0); return }
        let list = queue.isEmpty ? tracks : queue, index = (queue.isEmpty ? tracks : queue).firstIndex { $0.id == currentID } ?? -1
        let next = index + direction
        if list.indices.contains(next) { try await play(list[next], rows: list) }
        else if repeatMode == "all", let track = direction < 0 ? list.last : list.first { try await play(track, rows: list) }
    }
    private func applyNativeQueue() {
        if selectedDevice == "local" && (ownsSession || offlinePlayback), let id = currentID {
            if let command = try? NativeAudioCommand(["action": "queue", "queue": NativeValue.queue(queue, currentID: id), "repeat": repeatMode]) { audio.handle(command) }
        }
    }
    func setQueueMode(shuffled: Bool? = nil, repeatMode desiredMode: String? = nil) async throws {
        guard canEditQueue else { return }; queueSaving = true; defer { queueSaving = false }
        let nextShuffle = shuffled ?? shuffle, nextMode = desiredMode ?? repeatMode
        guard ["off", "one", "all"].contains(nextMode) else { throw XASSErr.invalidCommand }
        let original = baseQueue.isEmpty ? (queue.isEmpty ? tracks : queue) : baseQueue
        let ordered = shuffled == nil ? (queue.isEmpty ? original : queue) : nextShuffle ? original.shuffled() : original
        if !offlinePlayback, !canonicalSessionKey.isEmpty {
            _ = try await writeSession(["session_key": canonicalSessionKey, "queue": Array(ordered.prefix(2000)).map(\.id), "repeat_mode": nextMode, "takeover": false], explicit: true)
        }
        baseQueue = original; queue = ordered; shuffle = nextShuffle; repeatMode = nextMode; applyNativeQueue()
    }
    private func snapshot(state: String? = nil, position: Double? = nil) -> [String: Any] {
        var result: [String: Any] = ["session_key": sessionKey, "client_id": clientID, "device": selectedDevice,
            "takeover": false, "state": state ?? playbackState, "position": position ?? self.position,
            "share_site": shareSite, "share_discord": false, "output_id": outputID, "volume": Int(volume)]
        if let id = currentID { result["track_id"] = id }; return result
    }
    private func writeSession(_ body: [String: Any], explicit: Bool = false) async throws -> [String: Any] {
        if !explicit, let current = sessionWrite { return try await current.value }
        let previous = sessionWrite, operationID = UUID()
        let operation = Task {
            if let previous = previous { _ = try? await previous.value }
            try Task.checkCancellation()
            return try await api.request("/api/mini/music/session", method: "POST", body: body)
        }
        sessionWriteID = operationID
        sessionWrite = operation
        defer { if sessionWriteID == operationID { sessionWrite = nil } }
        return try await operation.value
    }
    private func queueReport(_ body: [String: Any]) {
        guard ownsSession, !suppressReports, !transferPending else { return }
        var value = body; value["client_id"] = clientID; value["volume"] = Int(volume); value["output_id"] = outputID
        // Sharing is a shared server setting, not a stale player heartbeat field.
        value.removeValue(forKey: "share_site"); value.removeValue(forKey: "share_discord")
        pendingReport = value
        guard reportTask == nil else { return }
        reportTask = Task { [weak self] in
            guard let self = self else { return }
            defer { self.reportTask = nil }
            while let next = self.pendingReport, self.ownsSession {
                self.pendingReport = nil
                do { _ = try await self.writeSession(next); await self.processInbox() }
                catch {
                    if let failure = error as? OwnerAPIError, failure.status == 409 {
                        self.generation = UUID(); self.ownsSession = false; self.pendingReport = nil; self.suppressReports = true
                        self.audio.pause(); let position = self.audio.exactPosition(); self.suppressReports = false
                        if failure.detail?["code"] as? String == "transfer_requested", let id = failure.detail?["transfer_id"] as? String {
                            do { _ = try await self.api.request("/api/mini/music/transfers/" + OwnerAPI.pathComponent(id) + "/ack", method: "POST", body: ["session_key": self.sessionKey, "position": position]) }
                            catch { self.handle(error) }
                        }
                    } else if (error as? OwnerAPIError)?.status == 401 {
                        self.generation = UUID(); self.ownsSession = false; self.pendingReport = nil
                        self.suppressReports = true; self.audio.pause(); self.suppressReports = false; self.handle(error)
                    }
                    // A network outage does not kill a locally buffered track.
                }
            }
        }
    }
    private func remoteControl(_ action: String, extra: [String: Any] = [:]) async throws {
        guard !busy, !canonicalSessionKey.isEmpty else { return }; busy = true
        defer { busy = false; notice = nil; transferStatus = nil; transferProgress = 0 }
        let nextGeneration = UUID(); generation = nextGeneration; transferCancelled = false
        var body: [String: Any] = ["target_key": canonicalSessionKey, "action": action]; body.merge(extra) { _, new in new }
        let response = try await api.request("/api/mini/music/session/control", method: "POST", body: body)
        guard let id = NativeValue.identifier(response["command_id"]) else { throw OwnerAPIError.invalidResponse }
        activeTransferID = id
        transferStatus = "Команда отправлена…"; transferProgress = 0.1
        notice = nil
        let deadline = Date().addingTimeInterval(30)
        let startedAt = Date()
        while Date() < deadline {
            if transferCancelled || generation != nextGeneration {
                try? await api.request("/api/mini/music/session/commands/" + OwnerAPI.pathComponent(id) + "/cancel", method: "POST", body: nil)
                activeTransferID = nil; throw CancellationError()
            }
            let elapsed = Date().timeIntervalSince(startedAt)
            transferProgress = min(0.92, 0.1 + elapsed / 30)
            transferStatus = elapsed < 10 ? "Ожидаю подтверждение устройства…" : "Ещё жду ответ устройства…"
            try await Task.sleep(for: .seconds(1)); try Task.checkCancellation()
            let result = try await api.request("/api/mini/music/session/commands/" + OwnerAPI.pathComponent(id), method: "GET", body: nil)
            let status = result["status"] as? String ?? "pending"
            if ["completed", "complete"].contains(status) {
                transferProgress = 1; transferStatus = "Готово"; activeTransferID = nil
                try await refreshSession(); return
            }
            if ["failed", "cancelled", "expired"].contains(status) {
                activeTransferID = nil
                throw OwnerAPIError(status: 409, message: result["error"] as? String ?? "Устройство не выполнило команду. Откройте XASS на нём и повторите.")
            }
        }
        activeTransferID = nil
        throw OwnerAPIError(status: 408, message: "Устройство не ответило. iOS не позволяет удалённо запустить приостановленное приложение: откройте XASS на нужном iPhone.")
    }
    private func processInbox() async {
        guard authorized, !inboxBusy, !offlinePlayback, !transferPending else { return }; inboxBusy = true; defer { inboxBusy = false }
        let inboxGeneration = generation
        do {
            let response = try await api.request("/api/mini/music/session/commands?session_key=" + OwnerAPI.pathComponent(sessionKey), method: "GET", body: nil)
            for command in response["commands"] as? [[String: Any]] ?? [] {
                guard inboxGeneration == generation, !transferPending else { return }
                guard let id = NativeValue.identifier(command["id"]), let action = command["action"] as? String else { continue }
                let ack: [String: Any]
                if let cached = acknowledgedCommands[id] { ack = cached }
                else {
                    guard NativeValue.number(command["expires_at"]) > Date().timeIntervalSince1970 else { continue }
                    if !audio.hasPlayableItem || selectedDevice != "local" || !ownsSession {
                        ack = ["session_key": sessionKey, "ok": false, "state": "stopped", "position": audio.exactPosition(), "error": "На этом iPhone нет активного трека. Откройте XASS и выберите воспроизведение."]
                    } else {
                        do {
                            var body: [String: Any] = ["action": action]
                            if let value = command["position"] { body["position"] = value }; if let value = command["volume"] { body["volume"] = value }
                            let value = try NativeAudioCommand(body)
                            if action == "seek" { await audio.seekConfirmed(value.position) }
                            else { audio.handle(value) }
                            if action == "volume" { volume = value.volume }
                            ack = ["session_key": sessionKey, "ok": audio.state != "error", "state": audio.state, "position": audio.exactPosition()]
                            // Always ACK before aborting on generation/transfer change — else seek/pause/next repeats.
                            if inboxGeneration != generation || transferPending || !ownsSession {
                                acknowledgedCommands[id] = ack
                                _ = try? await api.request("/api/mini/music/session/commands/" + OwnerAPI.pathComponent(id) + "/ack", method: "POST", body: ack)
                                return
                            }
                        } catch { ack = ["session_key": sessionKey, "ok": false, "state": audio.state, "position": audio.exactPosition(), "error": "Некорректная команда отклонена"] }
                    }
                    acknowledgedCommands[id] = ack
                    if acknowledgedCommands.count > 128, let oldest = acknowledgedCommands.keys.first { acknowledgedCommands.removeValue(forKey: oldest) }
                }
                _ = try await api.request("/api/mini/music/session/commands/" + OwnerAPI.pathComponent(id) + "/ack", method: "POST", body: ack)
            }
        } catch { if (error as? OwnerAPIError)?.status == 401 { authorized = false } }
    }
    private func waitForActionForeground() async throws {
        for _ in 0..<12 { if canSendActions() { return }; try await Task.sleep(for: .milliseconds(80)) }
        throw OwnerAPIError(status: 0, message: "Вернитесь в XASS и повторите подтверждение. Команда не отправлена.")
    }
    func setSharing(_ desired: Bool) async throws {
        guard !shareSaving, let id = currentID else { return }; shareSaving = true; defer { shareSaving = false }
        let state = try await api.request("/api/mini/music/session", method: "GET", body: nil)
        var body = state["session"] as? [String: Any] ?? [:]
        guard body["track_id"] as? Int == id, let key = body["session_key"] as? String, !key.isEmpty else { throw OwnerAPIError(status: 409, message: "Воспроизведение изменилось. Обновите плеер и повторите.") }
        body = ["session_key": key, "share_site": desired, "takeover": false]
        _ = try await writeSession(body, explicit: true); shareSite = desired
        if ownsSession && selectedDevice == "local" { audio.handle(try NativeAudioCommand(["action": "session", "session": snapshot()])) }
    }
    func control(_ action: String, extra: [String: Any] = [:], source: String? = nil) async throws -> [String: Any] {
        let name = source ?? (selectedDevice.hasPrefix("agent:") ? String(selectedDevice.dropFirst(6)) : "")
        guard !name.isEmpty else { throw OwnerAPIError(status: 400, message: "Выберите компьютер") }
        var body: [String: Any] = ["source_name": name, "action": action, "output_id": outputID, "position_sec": position, "volume": Int(volume)]
        body.merge(extra) { _, new in new }
        let started = try await api.request("/api/mini/music/control", method: "POST", body: body)
        guard let id = started["command_id"] as? Int else { throw OwnerAPIError.invalidResponse }
        let deadline = Date().addingTimeInterval(25)
        while Date() < deadline {
            let status = try await api.request("/api/mini/music/control/\(id)", method: "GET", body: nil)
            let result = status["result"] as? [String: Any] ?? [:]
            if status["status"] as? String == "completed" {
                if result["ok"] as? Bool == false { throw OwnerAPIError(status: 409, message: result["message"] as? String ?? "Агент не выполнил команду") }
                return result["details"] as? [String: Any] ?? [:]
            }
            if ["failed", "cancelled"].contains(status["status"] as? String ?? "") { throw OwnerAPIError(status: 409, message: result["message"] as? String ?? "Команда не выполнена") }
            try await Task.sleep(for: .milliseconds(700)); try Task.checkCancellation()
        }
        throw OwnerAPIError(status: 408, message: "ПК не ответил вовремя. Проверьте агент.")
    }
    func loadOutputs(source: String) async throws {
        let requestID = UUID(); outputRequest = requestID; outputs = []
        let result = try await control("outputs", source: source)
        guard requestID == outputRequest else { return }
        outputs = (result["outputs"] as? [[String: Any]] ?? []).compactMap(PlayerOutput.init)
    }
    func favorite(_ track: LibraryTrack) async throws {
        let result = try await api.request("/api/mini/music/tracks/\(track.id)", method: "PATCH", body: ["favorite": !track.favorite])
        if let value = result["track"] as? [String: Any], let updated = LibraryTrack(value), let index = tracks.firstIndex(where: { $0.id == track.id }) { tracks[index] = updated }
    }
    func savePlaylist(id: Int?, name: String, trackIDs: [Int]) async throws {
        let result = try await api.request("/api/mini/music/playlists" + (id.map { "/\($0)" } ?? ""), method: id == nil ? "POST" : "PUT", body: ["name": name.trimmingCharacters(in: .whitespacesAndNewlines), "track_ids": trackIDs])
        if let value = result["playlist"] as? [String: Any], let playlist = LibraryPlaylist(value) { playlists.removeAll { $0.id == playlist.id }; playlists.insert(playlist, at: 0) }
    }
    func deletePlaylist(_ playlist: LibraryPlaylist) async throws {
        _ = try await api.request("/api/mini/music/playlists/\(playlist.id)", method: "DELETE", body: nil); playlists.removeAll { $0.id == playlist.id }
    }
    func deleteTrack(_ track: LibraryTrack) async throws {
        _ = try await api.request("/api/mini/music/tracks/\(track.id)", method: "DELETE", body: nil)
        tracks.removeAll { $0.id == track.id }; queue.removeAll { $0.id == track.id }
        if currentID == track.id && ownsSession { audio.stop(); ownsSession = false; currentID = nil }
    }
    func download(_ track: LibraryTrack) async throws {
        let url = audio.cachedTracks.contains(where: { $0.id == track.id }) ? "" : try await ticket(track.id, purpose: "download")
        audio.handle(try NativeAudioCommand(["action": "download", "trackId": track.id, "url": url, "title": track.title, "artist": track.artist]))
    }
    func upload(_ url: URL) async throws {
        guard uploadName == nil else { return }
        let scoped = url.startAccessingSecurityScopedResource(); defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        guard size > 0, size <= maxUpload else { throw OwnerAPIError(status: 413, message: "Аудиофайл пустой или больше лимита сервера.") }
        uploadName = url.lastPathComponent; uploadProgress = 0; defer { uploadName = nil }
        let started = try await api.request("/api/mini/music/uploads", method: "POST", body: ["filename": url.lastPathComponent, "size": size])
        guard let id = started["upload_id"] as? String else { throw OwnerAPIError.invalidResponse }
        let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
        var offset = started["offset"] as? Int ?? 0
        do {
            while offset < size {
                try Task.checkCancellation(); try handle.seek(toOffset: UInt64(offset))
                let data = try handle.read(upToCount: min(512 * 1024, size - offset)) ?? Data()
                guard !data.isEmpty else { throw OwnerAPIError.invalidResponse }
                let result = try await api.request("/api/mini/music/uploads/" + OwnerAPI.pathComponent(id), method: "PUT", body: ["offset": offset, "data": data.base64EncodedString()])
                guard let next = result["offset"] as? Int, next > offset, next <= size else { throw OwnerAPIError.invalidResponse }
                offset = next; uploadProgress = Double(offset) / Double(size)
            }
            let result = try await api.request("/api/mini/music/uploads/" + OwnerAPI.pathComponent(id) + "/finish", method: "POST", body: nil)
            if let value = result["track"] as? [String: Any], let track = LibraryTrack(value) { tracks.removeAll { $0.id == track.id }; tracks.insert(track, at: 0) }
            if let values = result["tracks"] as? [[String: Any]] { let imported = values.compactMap(LibraryTrack.init); let ids = Set(imported.map(\.id)); tracks.removeAll { ids.contains($0.id) }; tracks.insert(contentsOf: imported, at: 0) }
            notice = result["archive"] as? Bool == true ? "Архив обработан. Добавлено: \(result["added"] as? Int ?? 0), дубликатов: \(result["duplicates"] as? Int ?? 0), пропущено: \(result["skipped"] as? Int ?? 0)." : "Трек добавлен в библиотеку"
        } catch {
            _ = try? await api.request("/api/mini/music/uploads/" + OwnerAPI.pathComponent(id), method: "DELETE", body: nil)
            throw error
        }
    }
    func enroll(pairInput: String) async throws {
        guard !busy else { return }; busy = true; confirmationActivity?(true)
        defer { busy = false; confirmationActivity?(false) }
        try await authorization.enroll(pairInput: pairInput, deviceName: UIDevice.current.name)
        showEnrollment = false; await refresh(); notice = "Этот iPhone привязан для защищённого управления"
    }
    func deviceCommand(_ device: NativeDevice, command: String) async throws {
        guard !busy else { return }; busy = true; confirmationActivity?(true)
        defer { busy = false; confirmationActivity?(false) }
        guard enrolled else { throw OwnerAPIError(status: 428, message: "Привяжите ключ iPhone для защищённых команд ПК.") }
        var payload: [String: Any] = [:]; if ["reboot", "shutdown"].contains(command) { payload["delay_sec"] = 0 }
        let proof = try await authorization.proof(purpose: "agent:\(command):\(device.name)", binding: ["source_id": device.id, "command": command, "payload": payload], reason: "Подтвердить действие с ПК \(device.name)")
        try await waitForActionForeground()
        _ = try await api.request("/api/mini/agents/" + OwnerAPI.pathComponent(device.name) + "/commands", method: "POST", body: ["command": command, "payload": payload, "action_proof": proof])
        notice = "Команда отправлена агенту. Выполнение зависит от подключения ПК."
    }
    func detach(_ device: NativeDevice) async throws {
        guard !busy else { return }; busy = true; confirmationActivity?(true)
        defer { busy = false; confirmationActivity?(false) }
        guard enrolled else { throw OwnerAPIError(status: 428, message: "Привяжите ключ iPhone для безопасной отвязки ПК.") }
        let proof = try await authorization.proof(purpose: "agent:detach:\(device.id):\(device.name)", binding: ["source_id": device.id, "confirm_name": device.name], reason: "Отвязать ПК \(device.name) от XASS")
        try await waitForActionForeground()
        _ = try await api.request("/api/mini/agents/" + OwnerAPI.pathComponent(device.name), method: "DELETE", body: ["source_id": device.id, "confirm_name": device.name, "action_proof": proof])
        devices.removeAll { $0.id == device.id }; notice = "ПК отвязан. Архивы на сервере сохранены."
    }
    func freeServerCopy(trackID: Int, source: String, sha256: String) async throws {
        guard !busy else { return }; busy = true; confirmationActivity?(true)
        defer { busy = false; confirmationActivity?(false) }
        guard enrolled else { throw OwnerAPIError(status: 428, message: "Привяжите защищённый ключ iPhone перед удалением серверной копии.") }
        let proof = try await authorization.proof(purpose: "music:evict:\(trackID):\(sha256)", binding: ["source_name": source, "sha256": sha256], reason: "Освободить копию музыки на сервере XASS")
        try await waitForActionForeground()
        _ = try await api.request("/api/mini/music/storage/\(trackID)/free-server-copy", method: "POST", body: ["source_name": source, "sha256": sha256, "confirm": "FREE SERVER COPY", "action_proof": proof])
        notice = "Серверная копия освобождена. Проверенная музыка хранится на выбранном ПК."
    }
}
