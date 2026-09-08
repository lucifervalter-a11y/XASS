import Foundation
import AVFoundation
import MediaPlayer
import Combine

struct DownloadedTrack: Codable, Identifiable {
    var id: Int
    var title: String
    var artist: String
    var duration: Double
    var trackID: Int { id }
    var bridge: [String: Any] { ["trackId": id, "title": title, "artist": artist, "duration": duration] }
}

struct OfflineLibrary {
    let directory: URL
    init(origin: ServerOrigin) throws {
        let root = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        directory = root.appendingPathComponent("XASSOffline", isDirectory: true).appendingPathComponent(origin.namespace, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var location = directory; var values = URLResourceValues(); values.isExcludedFromBackup = true
        try location.setResourceValues(values)
    }
    func file(_ id: Int) -> URL { directory.appendingPathComponent("\(id).audio") }
    func tracks() -> [DownloadedTrack] {
        guard let data = try? Data(contentsOf: directory.appendingPathComponent("index.json")),
              data.count <= 2 * 1024 * 1024,
              let items = try? JSONDecoder().decode([DownloadedTrack].self, from: data) else { return [] }
        return items.filter { $0.id > 0 && FileManager.default.fileExists(atPath: file($0.id).path) }
    }
    func save(_ items: [DownloadedTrack]) throws {
        let data = try JSONEncoder().encode(items)
        try data.write(to: directory.appendingPathComponent("index.json"), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
    func remove(_ track: DownloadedTrack) throws {
        guard track.id > 0 else { return }
        // The only removable path is an app-created numeric audio file in this origin's sandbox.
        let path = file(track.id)
        if FileManager.default.fileExists(atPath: path.path) { try FileManager.default.removeItem(at: path) }
        try save(tracks().filter { $0.id != track.id })
    }
}

struct NativeAudioCommand {
    let action: String
    let trackID: Int
    let position: Double
    let volume: Double
    let title: String
    let artist: String
    let rawURL: String
    let session: [String: Any]?
    static let actions: Set<String> = ["play", "pause", "resume", "stop", "seek", "volume", "download", "downloads", "route", "session"]
    init(_ body: [String: Any]) throws {
        guard let action = body["action"] as? String, Self.actions.contains(action),
              let encoded = try? JSONSerialization.data(withJSONObject: body), encoded.count <= 32 * 1024 else { throw XASSErr.invalidCommand }
        self.action = action
        let id = (body["trackId"] as? NSNumber)?.doubleValue ?? 0
        let position = (body["position"] as? NSNumber)?.doubleValue ?? 0
        let volume = (body["volume"] as? NSNumber)?.doubleValue ?? 70
        guard id.isFinite, id >= 0, id <= Double(Int32.max), id.rounded() == id,
              position.isFinite, position >= 0, position <= 86400,
              volume.isFinite, volume >= 0, volume <= 100,
              !["play", "download"].contains(action) || id > 0 else { throw XASSErr.invalidCommand }
        trackID = Int(id); self.position = position; self.volume = volume
        title = String((body["title"] as? String ?? "Музыка XASS").prefix(300))
        artist = String((body["artist"] as? String ?? "").prefix(300))
        rawURL = body["url"] as? String ?? ""
        session = body["session"] as? [String: Any]
    }
}

@MainActor final class AudioController: ObservableObject {
    @Published private(set) var state = "stopped"
    @Published private(set) var title = "Музыка XASS"
    @Published private(set) var artist = ""
    @Published private(set) var position: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var trackID = 0
    @Published private(set) var downloads: [DownloadedTrack] = []
    @Published private(set) var downloadIDs: Set<Int> = []
    @Published var error: String?
    @Published var showRoutes = false
    var emit: (([String: Any]) -> Void)?
    var reportSession: (([String: Any]) -> Void)?
    private let player = AVPlayer()
    private var loader: SecureMediaLoader?
    private var origin: ServerOrigin?
    private var library: OfflineLibrary?
    private var jobs: [Int: PrivateDownload] = [:]
    private var timeObserver: Any?
    private var itemObserver: NSKeyValueObservation?
    private var playerObserver: NSKeyValueObservation?
    private var observers: [NSObjectProtocol] = []
    private var sessionSnapshot: [String: Any]?
    private var lastReport = Date.distantPast
    private var resumeAfterInterruption = false

    init() {
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main) { [weak self] _ in
            Task { @MainActor in self?.updateTime() }
        }
        playerObserver = player.observe(\.timeControlStatus, options: [.new]) { [weak self] player, _ in
            Task { @MainActor in
                guard let self = self, self.player.currentItem != nil, self.state != "ended", self.state != "error" else { return }
                self.state = player.timeControlStatus == .playing ? "playing" : player.timeControlStatus == .waitingToPlayAtSpecifiedRate ? "loading" : "paused"
                self.publish()
            }
        }
        observers.append(NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: nil, queue: .main) { [weak self] note in
            Task { @MainActor in
                guard let self = self, let item = note.object as? AVPlayerItem, item === self.player.currentItem else { return }
                self.state = "ended"; self.publish(forceReport: true)
            }
        })
        observers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let type = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? NSNumber)?.uintValue
            let flags = (note.userInfo?[AVAudioSessionInterruptionOptionKey] as? NSNumber)?.uintValue ?? 0
            Task { @MainActor in
                guard let self = self else { return }
                if type == AVAudioSession.InterruptionType.began.rawValue { self.resumeAfterInterruption = self.state == "playing"; self.pause() }
                else if self.resumeAfterInterruption && flags & AVAudioSession.InterruptionOptions.shouldResume.rawValue != 0 { self.resume() }
            }
        })
        observers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] note in
            let reason = (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? NSNumber)?.uintValue
            if reason == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue { Task { @MainActor in self?.pause() } }
        })
        let remote = MPRemoteCommandCenter.shared()
        remote.playCommand.addTarget { [weak self] _ in Task { @MainActor in self?.resume() }; return .success }
        remote.pauseCommand.addTarget { [weak self] _ in Task { @MainActor in self?.pause() }; return .success }
        remote.togglePlayPauseCommand.addTarget { [weak self] _ in Task { @MainActor in guard let self = self else { return }; self.state == "playing" ? self.pause() : self.resume() }; return .success }
        remote.stopCommand.addTarget { [weak self] _ in Task { @MainActor in self?.stop() }; return .success }
        remote.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            Task { @MainActor in self?.seek(event.positionTime) }; return .success
        }
    }
    func configure(_ origin: ServerOrigin) {
        if self.origin == origin { return }
        stop(); jobs.values.forEach { $0.cancel() }; jobs.removeAll(); downloadIDs.removeAll()
        self.origin = origin; library = try? OfflineLibrary(origin: origin)
        downloads = library?.tracks() ?? []; sessionSnapshot = nil
    }
    func disconnect() {
        stop(); jobs.values.forEach { $0.cancel() }; jobs.removeAll(); downloadIDs.removeAll()
        origin = nil; library = nil; downloads = []; sessionSnapshot = nil
    }
    func handle(_ command: NativeAudioCommand) {
        if let snapshot = command.session { setSession(snapshot) }
        do {
            switch command.action {
            case "play": try play(command)
            case "pause": pause()
            case "resume": resume()
            case "stop": stop()
            case "seek": seek(command.position)
            case "volume": player.volume = Float(command.volume / 100); publish()
            case "download": try download(command)
            case "downloads": emit?(["action": "downloads", "downloads": downloads.map(\.bridge)])
            case "route": showRoutes = true
            case "session": publish(forceReport: true)
            default: throw XASSErr.invalidCommand
            }
        } catch {
            self.error = error.localizedDescription
            if command.action == "download" { emit?(["action": "download", "trackId": command.trackID, "downloaded": false, "error": "Не удалось сохранить трек. Проверьте связь и свободное место."]) }
            else { state = "error"; publish() }
        }
    }
    private func activateAudio() throws {
        try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
        try AVAudioSession.sharedInstance().setActive(true)
    }
    private func play(_ command: NativeAudioCommand) throws {
        guard let origin = origin else { throw XASSErr.invalidOrigin }
        let item: AVPlayerItem
        let nextLoader: SecureMediaLoader?
        if let library = library, downloads.contains(where: { $0.id == command.trackID }), FileManager.default.fileExists(atPath: library.file(command.trackID).path) {
            item = AVPlayerItem(url: library.file(command.trackID)); nextLoader = nil
        } else {
            let url = try origin.mediaURL(command.rawURL, trackID: command.trackID)
            let created = SecureMediaLoader(origin: origin, trackID: command.trackID, url: url)
            item = AVPlayerItem(asset: created.asset()); nextLoader = created
        }
        try activateAudio()
        player.pause(); player.replaceCurrentItem(with: nil); loader?.invalidate(); loader = nextLoader
        state = "loading"; title = command.title; artist = command.artist; trackID = command.trackID
        position = command.position; duration = 0; error = nil
        player.volume = Float(command.volume / 100)
        itemObserver = item.observe(\.status, options: [.new]) { [weak self] item, _ in
            Task { @MainActor in
                guard let self = self, item === self.player.currentItem else { return }
                if item.status == .failed { self.state = "error"; self.error = "Трек недоступен или формат не поддерживается iPhone. Попробуйте MP3 / M4A."; self.publish(forceReport: true) }
            }
        }
        player.replaceCurrentItem(with: item)
        if command.position > 0 { player.seek(to: CMTime(seconds: command.position, preferredTimescale: 600)) }
        player.play(); publish(forceReport: true)
    }
    func playOffline(_ track: DownloadedTrack) {
        sessionSnapshot = nil
        guard let command = try? NativeAudioCommand(["action": "play", "trackId": track.id, "title": track.title, "artist": track.artist]) else { return }
        handle(command)
    }
    func pause() { player.pause(); if trackID > 0 { state = "paused"; publish(forceReport: true) } }
    func resume() {
        guard player.currentItem != nil else { return }
        do { try activateAudio(); if state == "ended" { player.seek(to: .zero) }; state = "playing"; player.play(); publish(forceReport: true) }
        catch { self.error = "Не удалось включить аудио. Проверьте устройство вывода."; state = "error"; publish() }
    }
    func stop() {
        player.pause(); player.replaceCurrentItem(with: nil); itemObserver = nil; loader?.invalidate(); loader = nil
        state = "stopped"; position = 0; publish(forceReport: true)
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
    func seek(_ seconds: Double) {
        guard seconds.isFinite, seconds >= 0, player.currentItem != nil else { return }
        let value = duration > 0 ? min(seconds, duration) : min(seconds, 86400)
        player.seek(to: CMTime(seconds: value, preferredTimescale: 600)); position = value; publish(forceReport: true)
    }
    private func updateTime() {
        let current = player.currentTime().seconds, total = player.currentItem?.duration.seconds ?? 0
        position = current.isFinite ? max(0, current) : 0; duration = total.isFinite ? max(0, total) : 0
        if trackID > 0 { publish() }
    }
    private func publish(forceReport: Bool = false) {
        var event: [String: Any] = ["state": state, "position": position, "duration": duration, "trackId": trackID, "native": true]
        if let error = error { event["error"] = error }
        emit?(event)
        if player.currentItem != nil {
            MPNowPlayingInfoCenter.default().nowPlayingInfo = [MPMediaItemPropertyTitle: title, MPMediaItemPropertyArtist: artist,
                MPMediaItemPropertyPlaybackDuration: duration, MPNowPlayingInfoPropertyElapsedPlaybackTime: position,
                MPNowPlayingInfoPropertyPlaybackRate: state == "playing" ? 1.0 : 0.0]
        }
        if var snapshot = sessionSnapshot, forceReport || Date().timeIntervalSince(lastReport) >= 5 {
            snapshot["state"] = state; snapshot["position"] = position; snapshot["track_id"] = trackID
            lastReport = Date(); reportSession?(snapshot)
        }
    }
    private func setSession(_ value: [String: Any]) {
        guard let key = value["session_key"] as? String, key.count >= 16, key.count <= 64,
              key.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil else { return }
        var snapshot: [String: Any] = ["session_key": key, "device": "local", "takeover": false]
        if let share = value["share_site"] as? Bool { snapshot["share_site"] = share }
        if let share = value["share_discord"] as? Bool { snapshot["share_discord"] = share }
        sessionSnapshot = snapshot
    }
    private func download(_ command: NativeAudioCommand) throws {
        guard let origin = origin, let library = library else { throw XASSErr.invalidOrigin }
        guard jobs[command.trackID] == nil else { return }
        let url = try origin.mediaURL(command.rawURL, trackID: command.trackID)
        let free = try? library.directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage
        if let free = free, free < PrivateDownload.maxBytes + 32 * 1024 * 1024 { throw URLError(.cannotWriteToFile) }
        let job = PrivateDownload(origin: origin, trackID: command.trackID, destination: library.file(command.trackID))
        jobs[command.trackID] = job; downloadIDs.insert(command.trackID)
        job.start(url: url) { [weak self] result in
            Task { @MainActor in
                guard let self = self, self.origin == origin else { return }
                self.jobs.removeValue(forKey: command.trackID); self.downloadIDs.remove(command.trackID)
                do {
                    _ = try result.get()
                    var tracks = library.tracks().filter { $0.id != command.trackID }
                    tracks.append(DownloadedTrack(id: command.trackID, title: command.title, artist: command.artist, duration: self.trackID == command.trackID ? self.duration : 0))
                    try library.save(tracks); self.downloads = tracks
                    self.emit?(["action": "download", "trackId": command.trackID, "downloaded": true])
                } catch { self.error = "Загрузка не завершена. Проверьте сеть и свободное место, затем повторите."; self.emit?(["action": "download", "trackId": command.trackID, "downloaded": false, "error": self.error!]) }
            }
        }
    }
    func removeDownload(_ track: DownloadedTrack) {
        do { try library?.remove(track); downloads = library?.tracks() ?? []; emit?(["action": "downloads", "downloads": downloads.map(\.bridge)]) }
        catch { self.error = "Не удалось удалить локальную копию." }
    }
}
