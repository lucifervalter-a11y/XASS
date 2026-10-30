import Foundation
import AVFoundation
import MediaPlayer
import Combine
import UIKit

struct DownloadedTrack: Codable, Identifiable {
    var id: Int
    var title: String
    var artist: String
    var duration: Double
    var fileExtension: String? = nil
    var trackID: Int { id }
    var bridge: [String: Any] { ["trackId": id, "title": title, "artist": artist, "duration": duration] }
}

/// Fence asynchronous ticket refreshes separately from URLSession callbacks.
/// Cancelling/pausing a job or requesting a newer ticket retires the old reply;
/// neither a late success nor a late error may resurrect the removed download.
@MainActor final class DownloadTicketRefreshes {
    private var epochs: [UUID: UUID] = [:]
    func invalidate(_ id: UUID) { epochs.removeValue(forKey: id) }
    func invalidateAll() { epochs.removeAll() }
    func request<Value>(for id: UUID, stillCurrent: @escaping () -> Bool,
                 issue: (@escaping (Result<Value, Error>) -> Void) -> Void,
                 complete: @escaping (Result<Value, Error>) -> Void) {
        let epoch = UUID(); epochs[id] = epoch
        issue { [weak self] result in
            guard let self, self.epochs[id] == epoch else { return }
            self.epochs.removeValue(forKey: id)
            guard stillCurrent() else { return }
            complete(result)
        }
    }
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
    func file(_ id: Int, extension suffix: String = "audio") -> URL {
        let safe = ["audio", "mp3", "m4a", "wav", "flac", "ogg"].contains(suffix) ? suffix : "audio"
        return directory.appendingPathComponent("\(id).\(safe)")
    }
    func file(for track: DownloadedTrack) -> URL { file(track.id, extension: track.fileExtension ?? "audio") }
    func tracks() -> [DownloadedTrack] {
        guard let data = try? Data(contentsOf: directory.appendingPathComponent("index.json")),
              data.count <= 2 * 1024 * 1024,
              let items = try? JSONDecoder().decode([DownloadedTrack].self, from: data) else { return [] }
        return items.filter { $0.id > 0 && FileManager.default.fileExists(atPath: file(for: $0).path) }
    }
    func save(_ items: [DownloadedTrack]) throws {
        let data = try JSONEncoder().encode(items)
        try data.write(to: directory.appendingPathComponent("index.json"), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
    func remove(_ track: DownloadedTrack) throws {
        guard track.id > 0 else { return }
        // The only removable path is an app-created numeric audio file in this origin's sandbox.
        let path = file(for: track)
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
    let downloadExpiresAt: Date?
    let session: [String: Any]?
    let queue: NativePlaybackQueue?
    static let actions: Set<String> = ["play", "pause", "resume", "stop", "seek", "volume", "download", "downloads", "route", "session", "queue", "next", "previous"]
    init(_ body: [String: Any]) throws {
        guard let action = body["action"] as? String, Self.actions.contains(action),
              JSONSerialization.isValidJSONObject(body),
              let encoded = try? JSONSerialization.data(withJSONObject: body), encoded.count <= 256 * 1024 else { throw XASSErr.invalidCommand }
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
        if let raw = body["downloadExpiresAt"] {
            guard let value = raw as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
                  value.doubleValue.isFinite, value.doubleValue > 0,
                  value.doubleValue <= Date().timeIntervalSince1970 + DownloadTicket.maximumLifetime else { throw XASSErr.invalidCommand }
            downloadExpiresAt = Date(timeIntervalSince1970: value.doubleValue)
        } else { downloadExpiresAt = nil }
        session = body["session"] as? [String: Any]
        queue = body["queue"] == nil ? nil : try NativePlaybackQueue(body["queue"], repeatMode: body["repeat"] as? String ?? "off")
    }
}

struct NativePlaybackQueue {
    let tracks: [DownloadedTrack]
    let repeatMode: String
    init(_ raw: Any?, repeatMode: String = "off") throws {
        guard let items = raw as? [[String: Any]], items.count <= 200,
              ["off", "all", "one"].contains(repeatMode) else { throw XASSErr.invalidCommand }
        var tracks: [DownloadedTrack] = [], ids = Set<Int>()
        for item in items {
            guard let number = item["trackId"] as? NSNumber else { throw XASSErr.invalidCommand }
            let value = number.doubleValue
            guard value.isFinite, value > 0, value <= Double(Int32.max), value.rounded() == value,
                  ids.insert(Int(value)).inserted else { throw XASSErr.invalidCommand }
            tracks.append(DownloadedTrack(id: Int(value), title: String((item["title"] as? String ?? "Музыка XASS").prefix(300)),
                artist: String((item["artist"] as? String ?? "").prefix(300)), duration: 0))
        }
        self.tracks = tracks; self.repeatMode = repeatMode
    }
    func next(currentID: Int, direction: Int, automatic: Bool) -> DownloadedTrack? {
        guard let index = tracks.firstIndex(where: { $0.id == currentID }) else { return nil }
        if automatic && repeatMode == "one" { return tracks[index] }
        let next = index + (direction < 0 ? -1 : 1)
        if tracks.indices.contains(next) { return tracks[next] }
        if repeatMode == "all" { return direction < 0 ? tracks.last : tracks.first }
        return nil
    }
}

@MainActor final class AudioController: ObservableObject {
    @Published private(set) var state = "stopped"
    @Published private(set) var title = "Музыка XASS"
    @Published private(set) var artist = ""
    @Published private(set) var position: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var trackID = 0
    /// Application gain only. The visible MPVolumeView owns system volume.
    @Published private(set) var playbackGain: Double = 100
    @Published private(set) var downloads: [DownloadedTrack] = []
    @Published private(set) var downloadIDs: Set<Int> = []
    @Published private(set) var downloadQueue: [BackgroundDownload] = []
    @Published var error: String?
    @Published var showRoutes = false
    @Published private(set) var cachedTracks: [CachedAudio] = []
    @Published private(set) var cacheBytes: Int64 = 0
    @Published private(set) var cacheLimitMB = 256
    @Published private(set) var cacheWorking = false
    @Published private(set) var sleepTimerEndDate: Date?
    @Published private(set) var sleepTimerAtTrackEnd = false
    @Published private(set) var playbackRate: Float = 1.0
    private var sleepTimerTask: Task<Void, Never>?
    var emit: (([String: Any]) -> Void)?
    var reportSession: (([String: Any]) -> Void)?
    var requestTicket: ((Int, @escaping (Result<String, Error>) -> Void) -> Void)?
    var requestDownloadTicket: ((Int, @escaping (Result<DownloadTicket, Error>) -> Void) -> Void)?
    var canResumePlayback: () -> Bool = { true }
    private let player: AVPlayer
    private var loader: SecureMediaLoader?
    private var origin: ServerOrigin?
    private var library: OfflineLibrary?
    private var downloadObserver: NSObjectProtocol?
    private var pendingPinIDs: Set<Int> = []
    private let downloadTicketRefreshes = DownloadTicketRefreshes()
    private var timeObserver: Any?
    private var itemObserver: NSKeyValueObservation?
    private var playerObserver: NSKeyValueObservation?
    private var observers: [NSObjectProtocol] = []
    private var sessionSnapshot: [String: Any]?
    private var lastReport = Date.distantPast
    private var resumeAfterInterruption = false
    private var queue: NativePlaybackQueue?
    private var transition = UUID()
    private var awaitingTrack = false
    private var automaticCache: AudioCache?
    private var cacheGeneration = UUID()
    private var accountGeneration = UUID()
    private var cacheDownload: PrivateDownload?
    private var cacheDownloadID: Int?
    private var recordedPlay = false
    /// Artwork for the currently loaded AVPlayer item. It is deliberately
    /// track-bound so a late cover request can never replace the Lock Screen
    /// image of the next song.
    private var nowPlayingArtwork: MPMediaItemArtwork?
    private var nowPlayingArtworkImage: UIImage?
    private var nowPlayingArtworkTrackID = 0
    private var preparedHandoff: (id: String, item: AVPlayerItem, loader: SecureMediaLoader?, player: AVPlayer)?
    private var handoffPrerolled = false

    init(player: AVPlayer = AVPlayer()) {
        self.player = player
        downloadObserver = NotificationCenter.default.addObserver(forName: BackgroundDownloads.changed, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.refreshDownloads() }
        }
        player.volume = 1
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main) { [weak self] _ in
            Task { @MainActor in self?.updateTime() }
        }
        playerObserver = player.observe(\.timeControlStatus, options: [.new]) { [weak self] player, _ in
            Task { @MainActor in
                guard let self = self, self.player.currentItem != nil, !self.awaitingTrack, self.state != "ended", self.state != "error" else { return }
                self.state = player.timeControlStatus == .playing ? "playing" : player.timeControlStatus == .waitingToPlayAtSpecifiedRate ? "loading" : "paused"
                if self.state == "playing" { self.recordCachePlay() }
                self.publish()
            }
        }
        observers.append(NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: nil, queue: .main) { [weak self] note in
            Task { @MainActor in
                guard let self = self, !self.awaitingTrack, let item = note.object as? AVPlayerItem, item === self.player.currentItem else { return }
                if self.sleepTimerAtTrackEnd {
                    self.cancelSleepTimer()
                    self.pause()
                    self.state = "ended"
                    self.publish(forceReport: true)
                    return
                }
                self.state = "ended"; self.publish(forceReport: true); self.advance(direction: 1, automatic: true)
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
        remote.nextTrackCommand.addTarget { [weak self] _ in Task { @MainActor in self?.advance(direction: 1) }; return .success }
        remote.previousTrackCommand.addTarget { [weak self] _ in Task { @MainActor in self?.previous() }; return .success }
        remote.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            Task { @MainActor in self?.seek(event.positionTime) }; return .success
        }
    }
    func setSleepTimer(minutes: Int) {
        cancelSleepTimer()
        guard minutes > 0 else { return }
        let target = Date().addingTimeInterval(TimeInterval(minutes * 60))
        sleepTimerEndDate = target
        sleepTimerTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(minutes * 60) * 1_000_000_000)
            guard !Task.isCancelled, let self = self else { return }
            self.fadeOutAndPause()
            self.cancelSleepTimer()
        }
    }

    func setSleepTimerAtTrackEnd() {
        cancelSleepTimer()
        sleepTimerAtTrackEnd = true
    }

    func cancelSleepTimer() {
        sleepTimerTask?.cancel()
        sleepTimerTask = nil
        sleepTimerEndDate = nil
        sleepTimerAtTrackEnd = false
    }

    var sleepTimerRemainingText: String? {
        if sleepTimerAtTrackEnd { return "В конце этого трека" }
        guard let end = sleepTimerEndDate else { return nil }
        let remaining = max(0, Int(end.timeIntervalSinceNow))
        return String(format: "%d:%02d", remaining / 60, remaining % 60)
    }

    func setPlaybackRate(_ rate: Float) {
        let safe = min(2.0, max(0.5, rate))
        playbackRate = safe
        if state == "playing" { player.rate = safe }
    }

    func fadeOutAndPause(duration: TimeInterval = 1.5) {
        guard state == "playing" else { return }
        let steps = 15
        let stepDuration = duration / Double(steps)
        Task { @MainActor in
            for i in 1...steps {
                try? await Task.sleep(nanoseconds: UInt64(stepDuration * 1_000_000_000))
                guard self.state == "playing" else { break }
                let fraction = 1.0 - (Float(i) / Float(steps))
                self.player.volume = fraction
            }
            self.pause()
            self.player.volume = 1.0
        }
    }

    func configure(_ origin: ServerOrigin) {
        if self.origin == origin { return }
        stop()
        if let previous = self.origin { BackgroundDownloads.shared.suspend(origin: previous, forgetSecrets: false) }
        pendingPinIDs.removeAll(); downloadIDs.removeAll(); downloadQueue = []
        resetCache()
        self.origin = origin; library = try? OfflineLibrary(origin: origin)
        downloads = library?.tracks() ?? []; sessionSnapshot = nil
        BackgroundDownloads.shared.activate(origin); refreshDownloads()
        let token = cacheGeneration
        Task { [weak self] in
            do {
                let cache = try AudioCache(origin: origin), value = try await cache.load()
                guard let self = self, self.cacheGeneration == token, self.origin == origin else { return }
                self.automaticCache = cache; self.applyCache(value)
            } catch { /* Cache availability must never block streaming or pinned downloads. */ }
        }
    }
    func disconnect() {
        stop()
        if let origin { BackgroundDownloads.shared.suspend(origin: origin, forgetSecrets: true) }
        pendingPinIDs.removeAll(); downloadIDs.removeAll(); downloadQueue = []
        resetCache()
        origin = nil; library = nil; downloads = []; sessionSnapshot = nil
    }
    func handle(_ command: NativeAudioCommand) {
        if let snapshot = command.session { setSession(snapshot) }
        do {
            switch command.action {
            case "play": queue = command.queue; try play(command)
            case "pause": pause()
            case "resume": resume()
            case "stop": stop()
            case "seek": seek(command.position)
            case "volume": setPlaybackGain(command.volume); publish(forceReport: true)
            case "download": try download(command)
            case "downloads": emit?(["action": "downloads", "downloads": downloads.map(\.bridge)])
            case "route": showRoutes = true
            case "session": publish(forceReport: true)
            case "queue": queue = command.queue; publish()
            case "next": advance(direction: 1)
            case "previous": previous()
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
    private func play(_ command: NativeAudioCommand, preservingGain: Bool = false,
                      preparedItem: AVPlayerItem? = nil, preparedLoader: SecureMediaLoader? = nil) throws {
        guard let origin = origin else { throw XASSErr.invalidOrigin }
        let item: AVPlayerItem
        let nextLoader: SecureMediaLoader?
        if let preparedItem { item = preparedItem; nextLoader = preparedLoader }
        else if let library = library, let track = downloads.first(where: { $0.id == command.trackID }), FileManager.default.fileExists(atPath: library.file(for: track).path) {
            item = AVPlayerItem(url: library.file(for: track)); nextLoader = nil
        } else if let cache = automaticCache, let entry = cachedTracks.first(where: { $0.id == command.trackID }) {
            item = AVPlayerItem(url: cache.file(entry.id, suffix: entry.track.fileExtension ?? "audio")); nextLoader = nil
        } else {
            let url = try origin.mediaURL(command.rawURL, trackID: command.trackID)
            let created = SecureMediaLoader(origin: origin, trackID: command.trackID, url: url)
            item = AVPlayerItem(asset: created.asset()); nextLoader = created
        }
        try activateAudio()
        transition = UUID(); awaitingTrack = false
        player.pause(); player.replaceCurrentItem(with: nil); loader?.invalidate(); loader = nextLoader
        if trackID != command.trackID {
            nowPlayingArtwork = nil; nowPlayingArtworkImage = nil; nowPlayingArtworkTrackID = 0
        }
        state = "loading"; title = command.title; artist = command.artist; trackID = command.trackID; recordedPlay = false
        position = command.position; duration = 0; error = nil
        // A fresh local play/handoff never inherits a stale zero/70% server
        // gain behind the system slider. Queue advance preserves an explicit
        // remote attenuation until the user resets it.
        setPlaybackGain(preservingGain ? playbackGain : 100)
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
    /// Preroll an independent silent player; the current source keeps playing
    /// until the server explicitly requests its STOP receipt.
    func prepareHandoff(_ command: NativeAudioCommand, attempt: String) async throws {
        cancelPreparedHandoff()
        guard let origin else { throw XASSErr.invalidOrigin }
        let item: AVPlayerItem
        let mediaLoader: SecureMediaLoader?
        if let file = analysisFile(command.trackID) {
            item = AVPlayerItem(url: file); mediaLoader = nil
        } else {
            let url = try origin.mediaURL(command.rawURL, trackID: command.trackID)
            let created = SecureMediaLoader(origin: origin, trackID: command.trackID, url: url)
            item = AVPlayerItem(asset: created.asset()); mediaLoader = created
        }
        let silent = AVPlayer(playerItem: item)
        preparedHandoff = (attempt, item, mediaLoader, silent)
        handoffPrerolled = false
        let deadline = Date().addingTimeInterval(25)
        do {
            while silent.status != .readyToPlay || item.status != .readyToPlay {
                try Task.checkCancellation()
                guard preparedHandoff?.id == attempt, Date() < deadline, item.status != .failed, silent.status != .failed else {
                    throw OwnerAPIError(status: 415, message: "Не удалось подготовить трек на iPhone")
                }
                try await Task.sleep(for: .milliseconds(50))
            }
            silent.preroll(atRate: 1) { [weak self] completed in
                Task { @MainActor in
                    guard self?.preparedHandoff?.id == attempt else { return }
                    self?.handoffPrerolled = completed
                }
            }
            while !handoffPrerolled {
                try Task.checkCancellation()
                guard preparedHandoff?.id == attempt, Date() < deadline, item.status != .failed else {
                    throw OwnerAPIError(status: 408, message: "iPhone не подтвердил подготовку аудиобуфера")
                }
                try await Task.sleep(for: .milliseconds(50))
            }
        } catch { cancelPreparedHandoff(attempt: attempt); throw error }
    }
    func cancelPreparedHandoff(attempt: String? = nil) {
        guard attempt == nil || preparedHandoff?.id == attempt else { return }
        preparedHandoff?.player.cancelPendingPrerolls()
        preparedHandoff?.player.replaceCurrentItem(with: nil)
        preparedHandoff?.loader?.invalidate()
        preparedHandoff = nil; handoffPrerolled = false
    }
    func startPreparedHandoff(_ command: NativeAudioCommand, attempt: String) async throws {
        guard let prepared = preparedHandoff, prepared.id == attempt, handoffPrerolled else {
            throw OwnerAPIError(status: 409, message: "Подготовка устарела. Повторите передачу")
        }
        prepared.player.replaceCurrentItem(with: nil)
        preparedHandoff = nil; handoffPrerolled = false
        if let snapshot = command.session { setSession(snapshot) }
        queue = command.queue
        try play(command, preparedItem: prepared.item, preparedLoader: prepared.loader)
        let deadline = Date().addingTimeInterval(12)
        do {
            while player.timeControlStatus != .playing {
                try Task.checkCancellation()
                guard trackID == command.trackID, state != "error", Date() < deadline else {
                    throw OwnerAPIError(status: 408, message: "iPhone не подтвердил запуск звука")
                }
                try await Task.sleep(for: .milliseconds(50))
            }
        } catch { pause(); throw error }
    }
    func playOffline(_ track: DownloadedTrack) {
        stop()
        sessionSnapshot = nil
        var seen = Set<Int>()
        let available = (downloads + cachedTracks.map(\.track)).filter { seen.insert($0.id).inserted }
        let index = available.firstIndex(where: { $0.id == track.id }) ?? 0
        let start = min(index, max(0, available.count - 200))
        let window = available.dropFirst(start).prefix(200)
        guard let command = try? NativeAudioCommand(["action": "play", "trackId": track.id, "title": track.title, "artist": track.artist, "queue": window.map(\.bridge)]) else { return }
        handle(command)
    }
    func pause() { transition = UUID(); awaitingTrack = false; player.pause(); if trackID > 0 { state = "paused"; publish(forceReport: true) } }
    func resume() {
        guard canResumePlayback() else { return }
        transition = UUID(); awaitingTrack = false
        guard player.currentItem != nil else { return }
        do { try activateAudio(); if state == "ended" { player.seek(to: .zero) }; state = "playing"; player.play(); publish(forceReport: true) }
        catch { self.error = "Не удалось включить аудио. Проверьте устройство вывода."; state = "error"; publish() }
    }
    func stop() {
        cancelPreparedHandoff()
        transition = UUID(); awaitingTrack = false
        player.pause(); player.replaceCurrentItem(with: nil); itemObserver = nil; loader?.invalidate(); loader = nil
        state = "stopped"; position = 0; nowPlayingArtwork = nil; nowPlayingArtworkImage = nil; nowPlayingArtworkTrackID = 0
        publish(forceReport: true)
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
    func seek(_ seconds: Double) {
        guard seconds.isFinite, seconds >= 0, player.currentItem != nil else { return }
        transition = UUID(); awaitingTrack = false
        let value = duration > 0 ? min(seconds, duration) : min(seconds, 86400)
        player.seek(to: CMTime(seconds: value, preferredTimescale: 600)); position = value; publish(forceReport: true)
    }
    func exactPosition() -> Double {
        let current = player.currentTime().seconds
        return current.isFinite ? max(0, current) : position
    }
    var hasPlayableItem: Bool { player.currentItem != nil }
    /// Local-only analysis may read an already downloaded, app-owned audio
    /// file. Never resolves a ticket, URL, arbitrary path, or library ID alone.
    func analysisFile(_ id: Int) -> URL? {
        guard id > 0 else { return nil }
        let candidates: [URL] = [
            library.flatMap { library in downloads.first(where: { $0.id == id }).map { library.file(for: $0) } },
            automaticCache.flatMap { cache in cachedTracks.first(where: { $0.id == id }).map { cache.file(id, suffix: $0.track.fileExtension ?? "audio") } }
        ].compactMap { $0 }
        return candidates.first { file in
            guard file.isFileURL,
                  let values = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]),
                  values.isRegularFile == true, values.isSymbolicLink != true else { return false }
            return true
        }
    }
    func resetPlaybackGain() { setPlaybackGain(100); publish(forceReport: true) }
    /// Uses only an image already fetched through OwnerService's authenticated
    /// artwork route. The request handler returns that in-memory image; it does
    /// not perform network or file I/O from SpringBoard.
    func setNowPlayingArtwork(_ image: UIImage?, for requestedTrackID: Int) {
        guard requestedTrackID > 0, requestedTrackID == trackID else { return }
        if nowPlayingArtworkTrackID == requestedTrackID, image === nowPlayingArtworkImage { return }
        if let image, image.size.width > 0, image.size.height > 0 {
            nowPlayingArtwork = MPMediaItemArtwork(boundsSize: image.size) { _ in image }
            nowPlayingArtworkImage = image
            nowPlayingArtworkTrackID = requestedTrackID
        } else {
            nowPlayingArtwork = nil; nowPlayingArtworkImage = nil; nowPlayingArtworkTrackID = 0
        }
        if player.currentItem != nil { publish() }
    }
    private func setPlaybackGain(_ value: Double) {
        guard value.isFinite else { return }
        let bounded = min(100, max(0, value))
        player.volume = Float(bounded / 100)
        playbackGain = bounded
    }
    func seekConfirmed(_ seconds: Double) async {
        guard seconds.isFinite, seconds >= 0, player.currentItem != nil else { return }
        let request = UUID(); transition = request; awaitingTrack = false
        let value = duration > 0 ? min(seconds, duration) : min(seconds, 86400)
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            player.seek(to: CMTime(seconds: value, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero) { _ in done.resume() }
        }
        guard transition == request else { return }
        position = exactPosition(); publish(forceReport: true)
    }
    private func updateTime() {
        guard player.currentItem != nil else { return }
        let current = player.currentTime().seconds, total = player.currentItem?.duration.seconds ?? 0
        position = current.isFinite ? max(0, current) : 0; duration = total.isFinite ? max(0, total) : 0
        if trackID > 0 { publish() }
    }
    private func publish(forceReport: Bool = false) {
        MPRemoteCommandCenter.shared().nextTrackCommand.isEnabled = queue?.next(currentID: trackID, direction: 1, automatic: false) != nil
        MPRemoteCommandCenter.shared().previousTrackCommand.isEnabled = player.currentItem != nil
        var event: [String: Any] = ["state": state, "position": position, "duration": duration, "trackId": trackID, "native": true, "playback_gain": playbackGain]
        if let error = error { event["error"] = error }
        emit?(event)
        if player.currentItem != nil {
            var info: [String: Any] = [
                MPMediaItemPropertyTitle: title,
                MPMediaItemPropertyArtist: artist,
                MPMediaItemPropertyPlaybackDuration: duration,
                MPNowPlayingInfoPropertyElapsedPlaybackTime: position,
                MPNowPlayingInfoPropertyPlaybackRate: state == "playing" ? 1.0 : 0.0,
                MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue,
                MPNowPlayingInfoPropertyIsLiveStream: false,
                MPNowPlayingInfoPropertyExternalContentIdentifier: String(trackID)
            ]
            if nowPlayingArtworkTrackID == trackID, let artwork = nowPlayingArtwork {
                info[MPMediaItemPropertyArtwork] = artwork
            }
            MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        }
        if var snapshot = sessionSnapshot, forceReport || Date().timeIntervalSince(lastReport) >= 5 {
            snapshot["state"] = state; snapshot["position"] = position; snapshot["track_id"] = trackID; snapshot["volume"] = playbackGain
            lastReport = Date(); reportSession?(snapshot)
        }
    }
    private func previous() {
        if position > 3 { seek(0) } else { advance(direction: -1) }
    }
    private func advance(direction: Int, automatic: Bool = false) {
        guard canResumePlayback() else { return }
        guard let track = queue?.next(currentID: trackID, direction: direction, automatic: automatic) else { return }
        let generation = UUID(); transition = generation
        let start: (String) -> Void = { [weak self] url in
            guard let self = self, self.transition == generation else { return }
            do {
                let command = try NativeAudioCommand(["action": "play", "trackId": track.id, "title": track.title, "artist": track.artist, "url": url])
                try self.play(command, preservingGain: true)
            } catch { self.state = "error"; self.error = "Не удалось включить следующий трек. Проверьте сеть или сохраните очередь на iPhone."; self.publish(forceReport: true) }
        }
        if let library = library, let saved = downloads.first(where: { $0.id == track.id }), FileManager.default.fileExists(atPath: library.file(for: saved).path) { start(""); return }
        if cachedTracks.contains(where: { $0.id == track.id }) { start(""); return }
        guard let request = requestTicket else { state = "error"; error = "Для следующего трека нужен вход на сервер."; publish(forceReport: true); return }
        awaitingTrack = true; player.pause(); state = "loading"; publish()
        request(track.id) { [weak self] result in
            Task { @MainActor in
                guard let self = self, self.transition == generation else { return }
                switch result {
                case .success(let url): start(url)
                case .failure: self.awaitingTrack = false; self.state = "error"; self.error = "Следующий трек недоступен. Проверьте сеть или войдите заново."; self.publish(forceReport: true)
                }
            }
        }
    }
    private func setSession(_ value: [String: Any]) {
        guard let key = value["session_key"] as? String, key.count >= 16, key.count <= 64,
              key.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil else { return }
        var snapshot: [String: Any] = ["session_key": key, "device": "local", "takeover": false]
        if let client = value["client_id"] as? String, client.count <= 160 { snapshot["client_id"] = client }
        if let share = value["share_site"] as? Bool { snapshot["share_site"] = share }
        if let share = value["share_discord"] as? Bool { snapshot["share_discord"] = share }
        sessionSnapshot = snapshot
    }
    private func download(_ command: NativeAudioCommand) throws {
        guard let origin = origin, let library = library else { throw XASSErr.invalidOrigin }
        guard !downloadIDs.contains(command.trackID) else { return }
        for item in BackgroundDownloads.shared.entries(for: origin) where item.track.id == command.trackID {
            downloadTicketRefreshes.invalidate(item.id)
        }
        if let cache = automaticCache, let entry = cachedTracks.first(where: { $0.id == command.trackID }) {
            pendingPinIDs.insert(command.trackID); refreshDownloads()
            let account = accountGeneration
            Task { [weak self] in
                do {
                    let copied = try await cache.copyToPinned(entry, to: library)
                    guard let self = self, self.origin == origin, self.accountGeneration == account else { return }
                    var values = library.tracks().filter { $0.id != copied.id }; values.append(copied); try library.save(values)
                    self.pendingPinIDs.remove(command.trackID); self.refreshDownloads()
                    self.emit?(["action": "download", "trackId": command.trackID, "downloaded": true])
                } catch {
                    guard let self = self, self.accountGeneration == account else { return }
                    self.pendingPinIDs.remove(command.trackID); self.refreshDownloads(); self.error = "Не удалось сохранить трек из кэша."
                }
            }
            return
        }
        if cacheDownloadID == command.trackID { cacheGeneration = UUID(); cacheDownload?.cancel(); cacheDownload = nil; cacheDownloadID = nil }
        let url = try origin.mediaURL(command.rawURL, trackID: command.trackID)
        let free = try? library.directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage
        if let free = free, free < PrivateDownload.maxBytes + 32 * 1024 * 1024 { throw URLError(.cannotWriteToFile) }
        let track = DownloadedTrack(id: command.trackID, title: command.title, artist: command.artist,
            duration: self.trackID == command.trackID ? self.duration : 0)
        try BackgroundDownloads.shared.enqueue(origin: origin, track: track, url: url, expiresAt: command.downloadExpiresAt)
        refreshDownloads()
    }
    private func refreshDownloads() {
        guard let origin else { return }
        downloadQueue = BackgroundDownloads.shared.entries(for: origin)
        downloadIDs = Set(downloadQueue.filter(\.active).map { $0.track.id }).union(pendingPinIDs)
        downloads = library?.tracks() ?? []
        emit?(["action": "downloads", "downloads": downloads.map(\.bridge)])
    }
    func pauseDownload(_ id: UUID) {
        downloadTicketRefreshes.invalidate(id); BackgroundDownloads.shared.pause(id); refreshDownloads()
    }
    func cancelDownload(_ id: UUID) {
        downloadTicketRefreshes.invalidate(id); BackgroundDownloads.shared.cancel(id); refreshDownloads()
    }
    func retryDownload(_ item: BackgroundDownload) {
        guard let origin, item.origin == origin.url.absoluteString,
              let current = BackgroundDownloads.shared.entries(for: origin).first(where: { $0.id == item.id }),
              !current.active else { return }
        downloadTicketRefreshes.invalidate(item.id)
        if BackgroundDownloads.shared.resume(item.id) { refreshDownloads(); return }
        guard let requestDownloadTicket else { error = "Для новой ссылки подключитесь к серверу."; return }
        let account = accountGeneration
        downloadTicketRefreshes.request(for: item.id, stillCurrent: { [weak self] in
            guard let self, self.origin == origin, self.accountGeneration == account,
                  let saved = BackgroundDownloads.shared.entries(for: origin).first(where: { $0.id == item.id }) else { return false }
            return !saved.active
        }, issue: { done in requestDownloadTicket(item.track.id, done) }) { [weak self] result in
            guard let self else { return }
            do {
                let ticket = try result.get()
                let url = try origin.mediaURL(ticket.url, trackID: item.track.id)
                try BackgroundDownloads.shared.enqueue(origin: origin, track: item.track, url: url, expiresAt: ticket.expiresAt)
                self.refreshDownloads()
            } catch { self.error = "Не удалось продолжить загрузку. Проверьте связь с сервером." }
        }
    }
    func removeDownload(_ track: DownloadedTrack) {
        do { try library?.remove(track); downloads = library?.tracks() ?? []; emit?(["action": "downloads", "downloads": downloads.map(\.bridge)]) }
        catch { self.error = "Не удалось удалить локальную копию." }
    }
    private func resetCache() {
        downloadTicketRefreshes.invalidateAll()
        accountGeneration = UUID(); cacheGeneration = UUID(); cacheDownload?.cancel(); cacheDownload = nil; cacheDownloadID = nil
        automaticCache = nil; cachedTracks = []; cacheBytes = 0; recordedPlay = false
    }
    private func applyCache(_ value: AudioCacheSnapshot) {
        cachedTracks = value.entries; cacheBytes = value.bytes; cacheLimitMB = value.limitMB
        emit?(["action": "cache", "bytes": value.bytes, "count": value.entries.count])
    }
    func setCacheLimit(_ megabytes: Int) async throws {
        guard let cache = automaticCache, !cacheWorking else { return }; cacheWorking = true; defer { cacheWorking = false }
        cacheGeneration = UUID(); cacheDownload?.cancel(); cacheDownload = nil; cacheDownloadID = nil
        let token = cacheGeneration, value = try await cache.setLimit(megabytes, protectedID: trackID)
        guard token == cacheGeneration else { return }; applyCache(value)
    }
    func clearAutomaticCache() async throws {
        guard let cache = automaticCache, !cacheWorking else { return }; cacheWorking = true; defer { cacheWorking = false }
        cacheGeneration = UUID(); cacheDownload?.cancel(); cacheDownload = nil; cacheDownloadID = nil; cachedTracks = []; cacheBytes = 0
        let token = cacheGeneration, value = try await cache.clear()
        guard token == cacheGeneration else { return }; applyCache(value)
    }
    private func recordCachePlay() {
        guard !recordedPlay, trackID > 0, let cache = automaticCache else { return }; recordedPlay = true
        let id = trackID, title = title, artist = artist, length = duration, token = cacheGeneration
        Task { [weak self] in
            do {
                let needed = try await cache.recordPlay(id)
                guard let self = self, self.cacheGeneration == token else { return }
                self.applyCache(await cache.snapshot())
                guard needed, await cache.hasSpaceForDownload(), self.cacheGeneration == token,
                      !self.downloads.contains(where: { $0.id == id }), !self.downloadIDs.contains(id),
                      self.cacheDownloadID == nil, let request = self.requestTicket, let origin = self.origin else { return }
                self.cacheDownloadID = id
                request(id) { [weak self] result in
                    guard let self = self, self.cacheGeneration == token, self.cacheDownloadID == id else { return }
                    do {
                        let url = try origin.mediaURL(result.get(), trackID: id)
                        let job = PrivateDownload(origin: origin, trackID: id, destination: cache.file(id))
                        self.cacheDownload = job
                        job.start(url: url) { [weak self] result in
                            Task { @MainActor in
                                guard let self = self, self.cacheGeneration == token else { return }
                                self.cacheDownload = nil; self.cacheDownloadID = nil
                                do {
                                    let file = try result.get()
                                    let entry = DownloadedTrack(id: id, title: title, artist: artist, duration: length, fileExtension: file.pathExtension)
                                    let snapshot = try await cache.insert(entry, protectedID: self.trackID)
                                    guard self.cacheGeneration == token else { return }; self.applyCache(snapshot)
                                } catch { /* An optional cache miss does not fail the active player. */ }
                            }
                        }
                    } catch { self.cacheDownload = nil; self.cacheDownloadID = nil }
                }
            } catch { /* No user action is required for an optional automatic cache. */ }
        }
    }
}
