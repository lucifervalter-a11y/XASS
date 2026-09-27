import XCTest
import SwiftUI
import MediaPlayer
import AVFoundation
@testable import XASS

@MainActor private final class VolumeOwnerFixture: OwnerService {
    let origin = try! ServerOrigin("https://volume-\(UUID().uuidString.lowercased()).invalid")
    var requests: [(String, String, [String: Any]?)] = []
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method, body))
        if path == "/api/mini/music/control", method == "POST" { return ["ok": true, "command_id": 123] }
        if path == "/api/mini/music/control/123" { return ["ok": true, "status": "completed", "result": ["ok": true, "details": [:]]] }
        if path == "/api/mini/music/session", method == "POST" { return ["ok": true, "session": body ?? [:]] }
        if path.hasPrefix("/api/mini/music/session/commands?") { return ["ok": true, "commands": []] }
        throw OwnerAPIError(status: 400, message: "Unexpected volume fixture request")
    }
}

final class NativeSystemVolumeTests: XCTestCase {
    func testVolumeTargetsNeverConfuseRemoteGainWithSystemVolume() {
        XCTAssertEqual(NativeVolumeTarget(device: "local", otherLocal: false), .system)
        XCTAssertEqual(NativeVolumeTarget(device: "local", otherLocal: true), .remotePlayer)
        XCTAssertEqual(NativeVolumeTarget(device: "agent:Fixture", otherLocal: false), .pcPlayer)
        XCTAssertEqual(NativeVolumeTarget(device: "agent:Fixture", otherLocal: true), .pcPlayer)
        XCTAssertEqual(NativeVolumeTarget.remotePlayer.title, "Уровень XASS на другом iPhone")
    }

    @MainActor func testSystemControlIsAnActualVisibleMPVolumeView() {
        let view = NativeSystemVolumeView.makeVolumeView()
        XCTAssertTrue(view.showsVolumeSlider)
        XCTAssertFalse(view.showsRouteButton)
        XCTAssertFalse(view.isHidden)
        XCTAssertEqual(view.alpha, 1)
        XCTAssertTrue(view.isUserInteractionEnabled)
        XCTAssertFalse(view.isAccessibilityElement, "Do not hide the native adjustable slider behind its parent")
        XCTAssertGreaterThanOrEqual(view.bounds.height, 44)
    }

    @MainActor func testLocalBusyPlayerKeepsSystemControlInVisibleHierarchy() async {
        let api = VolumeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        store.busy = true
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 320, height: 280))
        let host = UIHostingController(rootView: NativePlayerVolumeSection(store: store).padding(20))
        window.rootViewController = host; window.makeKeyAndVisible()
        defer { window.isHidden = true; window.rootViewController = nil; store.disconnect() }
        await Task.yield(); host.view.layoutIfNeeded()
        let controls = descendants(of: MPVolumeView.self, in: host.view)
        XCTAssertEqual(controls.count, 1, "The visible local section must embed the public system volume control")
        XCTAssertTrue(controls.first?.window === window)
        XCTAssertGreaterThanOrEqual(controls.first?.bounds.width ?? 0, 100)
        XCTAssertEqual(controls.first?.isHidden, false)
        XCTAssertEqual(controls.first?.isUserInteractionEnabled, true, "Network work must not disable hardware volume")
        XCTAssertTrue(api.requests.isEmpty, "Rendering hardware volume never talks to the server")
        // Apple explicitly does not support changing system volume in Simulator.
        // This verifies the real control and layout, not hardware-button behavior.
    }

    @MainActor func testPCSectionDoesNotEmbedPhoneSystemVolume() async {
        let api = VolumeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        store.selectedDevice = "agent:Fixture"
        let host = UIHostingController(rootView: NativePlayerVolumeSection(store: store))
        host.loadViewIfNeeded(); host.view.frame = CGRect(x: 0, y: 0, width: 320, height: 160)
        await Task.yield(); host.view.layoutIfNeeded()
        XCTAssertTrue(descendants(of: MPVolumeView.self, in: host.view).isEmpty)
        XCTAssertTrue(api.requests.isEmpty)
        store.disconnect()
    }

    @MainActor func testRemoteGainIsActualAVPlayerMultiplierAndExplicitlyReported() throws {
        let player = AVPlayer(), audio = AudioController(player: player)
        var event: [String: Any] = [:]
        audio.emit = { event = $0 }
        audio.handle(try NativeAudioCommand(["action": "volume", "volume": 24]))
        XCTAssertEqual(player.volume, 0.24, accuracy: 0.001)
        XCTAssertEqual(audio.playbackGain, 24)
        XCTAssertEqual(event["playback_gain"] as? Double, 24)
        XCTAssertNil(event["system_volume"], "Gain must not claim to be hardware volume")
        audio.resetPlaybackGain()
        XCTAssertEqual(player.volume, 1)
        XCTAssertEqual(audio.playbackGain, 100)
        audio.stop()
    }

    @MainActor func testFreshLocalPlayDoesNotInheritStaleRemoteZero() throws {
        let origin = try ServerOrigin("https://volume-start-\(UUID().uuidString.lowercased()).invalid")
        let library = try OfflineLibrary(origin: origin), tracks = try installSilentTracks(in: library)
        defer { try? FileManager.default.removeItem(at: library.directory) }
        let player = AVPlayer(), audio = AudioController(player: player)
        audio.configure(origin)
        defer { audio.disconnect() }
        XCTAssertEqual(audio.analysisFile(tracks[0].id), library.file(for: tracks[0]))
        XCTAssertNil(audio.analysisFile(999))
        XCTAssertNil(audio.analysisFile(-1))
        audio.handle(try NativeAudioCommand(["action": "volume", "volume": 0]))
        XCTAssertEqual(player.volume, 0)
        audio.handle(try NativeAudioCommand(["action": "play", "trackId": tracks[0].id, "volume": 0]))
        XCTAssertTrue(audio.hasPlayableItem)
        XCTAssertEqual(player.volume, 1, "Stale server zero must not mute a newly started local track behind MPVolumeView")
        XCTAssertEqual(audio.playbackGain, 100)
        try FileManager.default.removeItem(at: library.file(for: tracks[0]))
        XCTAssertNil(audio.analysisFile(tracks[0].id), "A stale saved index must not authorize a missing analysis file")
        try FileManager.default.createSymbolicLink(at: library.file(for: tracks[0]), withDestinationURL: library.file(for: tracks[1]))
        XCTAssertNil(audio.analysisFile(tracks[0].id), "Analysis must not follow a substituted symbolic link")
    }

    @MainActor func testQueueAdvancePreservesExplicitRemoteGainUntilReset() throws {
        let origin = try ServerOrigin("https://volume-queue-\(UUID().uuidString.lowercased()).invalid")
        let library = try OfflineLibrary(origin: origin), tracks = try installSilentTracks(in: library)
        defer { try? FileManager.default.removeItem(at: library.directory) }
        let player = AVPlayer(), audio = AudioController(player: player)
        audio.configure(origin)
        defer { audio.disconnect() }
        audio.playOffline(tracks[0])
        audio.handle(try NativeAudioCommand(["action": "volume", "volume": 18]))
        audio.handle(try NativeAudioCommand(["action": "next"]))
        XCTAssertEqual(audio.trackID, tracks[1].id)
        XCTAssertEqual(player.volume, 0.18, accuracy: 0.001)
        XCTAssertEqual(audio.playbackGain, 18)
        audio.resetPlaybackGain()
        XCTAssertEqual(player.volume, 1)
    }

    @MainActor func testPCGainCommandLeavesLocalPlayerAloneAndRejectsNonFiniteValues() async throws {
        let api = VolumeOwnerFixture(), player = AVPlayer(), audio = AudioController(player: player)
        let store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        store.selectedDevice = "agent:Fixture"
        try await store.setVolume(42)
        XCTAssertEqual(store.volume, 42)
        XCTAssertEqual(api.requests.first?.2?["action"] as? String, "volume")
        XCTAssertEqual(api.requests.first?.2?["volume"] as? Int, 42)
        XCTAssertEqual(player.volume, 1)
        let count = api.requests.count
        for value in [Double.nan, Double.infinity, -Double.infinity] {
            do { try await store.setVolume(value); XCTFail("Non-finite gain must be rejected") }
            catch { }
        }
        XCTAssertEqual(api.requests.count, count)
        store.resetLocalPlaybackGain()
        XCTAssertEqual(store.volume, 42, "Local reset must not touch a selected PC")
    }

    @MainActor func testLocalClaimAndGainResetDoNotReuseStalePCVolume() async throws {
        let api = VolumeOwnerFixture(), library = try OfflineLibrary(origin: api.origin)
        let tracks = try installSilentTracks(in: library)
        defer { try? FileManager.default.removeItem(at: library.directory) }
        let player = AVPlayer(), audio = AudioController(player: player)
        let store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        store.volume = 0
        try await store.play(LibraryTrack(tracks[0]))
        let claim = api.requests.first { $0.0 == "/api/mini/music/session" && $0.1 == "POST" }
        XCTAssertEqual(claim?.2?["volume"] as? Int, 100)
        XCTAssertEqual(store.volume, 100)
        XCTAssertEqual(player.volume, 1)
        audio.handle(try NativeAudioCommand(["action": "volume", "volume": 0]))
        XCTAssertEqual(store.volume, 0, "An actual remote attenuation updates the local gain notice")
        store.resetLocalPlaybackGain()
        XCTAssertEqual(store.volume, 100)
        XCTAssertEqual(player.volume, 1)
    }

    @MainActor private func descendants<T: UIView>(of type: T.Type, in view: UIView) -> [T] {
        (view as? T).map { [$0] } ?? view.subviews.flatMap { descendants(of: type, in: $0) }
    }

    private func installSilentTracks(in library: OfflineLibrary) throws -> [DownloadedTrack] {
        let tracks = [1, 2].map { DownloadedTrack(id: $0, title: "Silent volume fixture", artist: "Test", duration: 2, fileExtension: "wav") }
        let sampleRate: UInt32 = 8000, byteCount: UInt32 = 32000
        var bytes = Data()
        func text(_ value: String) { bytes.append(Data(value.utf8)) }
        func u16(_ value: UInt16) { var value = value.littleEndian; withUnsafeBytes(of: &value) { bytes.append(contentsOf: $0) } }
        func u32(_ value: UInt32) { var value = value.littleEndian; withUnsafeBytes(of: &value) { bytes.append(contentsOf: $0) } }
        text("RIFF"); u32(byteCount + 36); text("WAVEfmt "); u32(16); u16(1); u16(1)
        u32(sampleRate); u32(sampleRate * 2); u16(2); u16(16); text("data"); u32(byteCount)
        bytes.append(Data(repeating: 0, count: Int(byteCount)))
        for track in tracks { try bytes.write(to: library.file(for: track), options: .atomic) }
        try library.save(tracks)
        return tracks
    }
}
