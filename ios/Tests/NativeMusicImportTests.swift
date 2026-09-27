import XCTest
@testable import XASS

@MainActor private final class MusicImportOwnerFixture: OwnerService {
    let origin = try! ServerOrigin("https://music-import-fixture.invalid")
    var requests: [(String, String, [String: Any]?)] = []
    var handler: ((String, String, [String: Any]?) async throws -> [String: Any]?)?
    var audioLimit = 128 * 1024 * 1024
    var archiveLimit: Int? = 512 * 1024 * 1024
    private var sequence = 0
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method, body))
        if let response = try await handler?(path, method, body) { return response }
        if path == "/api/mini/bootstrap" { return ["ok": true, "sources": []] }
        if path.contains("/library") {
            var value: [String: Any] = ["ok": true, "tracks": [], "playlists": [], "max_upload_bytes": audioLimit]
            if let limit = archiveLimit { value["max_archive_upload_bytes"] = limit }
            return value
        }
        if path == "/api/mini/music/session" { return ["ok": true, "session": [:]] }
        if path.contains("/players") { return ["ok": true, "players": []] }
        if path == "/api/mini/music/uploads", method == "POST" {
            sequence += 1
            return ["ok": true, "upload_id": "fixture-\(sequence)", "offset": 0, "chunk_bytes": NativeMusicImportPolicy.chunkBytes]
        }
        if method == "PUT" {
            let count = Data(base64Encoded: body?["data"] as? String ?? "")?.count ?? 0
            return ["ok": true, "offset": (body?["offset"] as? Int ?? 0) + count]
        }
        if path.hasSuffix("/finish"), method == "POST" { return ["ok": true, "track": ["id": sequence, "title": "Fixture \(sequence)"]] }
        if method == "DELETE" { return ["ok": true] }
        if method == "GET", path.contains("/uploads/") { return ["ok": true, "status": "ready_to_finish"] }
        throw OwnerAPIError(status: 400, message: "Unexpected fixture operation")
    }
}

final class NativeMusicImportTests: XCTestCase {
    private func directory() throws -> URL {
        let path = FileManager.default.temporaryDirectory.appendingPathComponent("XASSImportTest-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: path, withIntermediateDirectories: false)
        return path
    }
    private func file(_ directory: URL, _ name: String, bytes: Int = 120) throws -> URL {
        let path = directory.appendingPathComponent(name)
        try Data(repeating: 0x31, count: bytes).write(to: path)
        return path
    }
    @MainActor private func waitForImport(_ store: NativeStore) async throws {
        let deadline = Date().addingTimeInterval(10)
        while store.isMusicImporting, Date() < deadline { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertFalse(store.isMusicImporting, "The bounded fixture import must finish")
    }
    func testSecurityScopeLeaseStartsSynchronouslyAndClosesExactlyOnce() {
        let url = URL(fileURLWithPath: "/fixture.mp3")
        var acquired = 0, released = 0
        var lease: NativeMusicImportSelection? = NativeMusicImportSelection([url], acquire: { _ in acquired += 1; return true }, release: { _ in released += 1 })
        XCTAssertEqual(acquired, 1); XCTAssertEqual(released, 0)
        lease?.close(); lease?.close(); lease = nil
        XCTAssertEqual(released, 1)
    }
    func testStagingReadsBoundedChunksPreservesOriginalAndRemovesPrivateCopy() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let source = try file(folder, "original.mp3", bytes: NativeMusicImportPolicy.chunkBytes + 37)
        let staged = try await NativeMusicImportFile.stage(source, fileLimit: 2 * 1024 * 1024, archiveLimit: 3 * 1024 * 1024)
        XCTAssertEqual(staged.size, NativeMusicImportPolicy.chunkBytes + 37)
        let first = try await staged.read(offset: 0, count: NativeMusicImportPolicy.chunkBytes)
        let last = try await staged.read(offset: first.count, count: 37)
        XCTAssertEqual(first.count, 512 * 1024); XCTAssertEqual(last.count, 37)
        do { _ = try await staged.read(offset: 0, count: NativeMusicImportPolicy.chunkBytes + 1); XCTFail("Oversized chunk accepted") } catch {}
        await staged.close()
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged.file.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged.file.deletingLastPathComponent().path))
        XCTAssertEqual(try Data(contentsOf: source).count, staged.size)
        do { _ = try await staged.read(offset: 0, count: 1); XCTFail("Missing staged file must fail") } catch {}
    }
    func testStagingRejectsDirectorySymlinkRemoteAndOverLimitBeforeNetwork() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let source = try file(folder, "source.mp3", bytes: 300)
        let link = folder.appendingPathComponent("link.mp3"); try FileManager.default.createSymbolicLink(at: link, withDestinationURL: source)
        for url in [folder, link, URL(string: "https://fixture.invalid/audio.mp3")!] {
            do { _ = try await NativeMusicImportFile.stage(url, fileLimit: 1000, archiveLimit: 1000); XCTFail("Unsupported source accepted") } catch {}
        }
        do { _ = try await NativeMusicImportFile.stage(source, fileLimit: 100, archiveLimit: 100); XCTFail("Oversized audio accepted") } catch {}
        let zip = try file(folder, "archive.zip", bytes: 300)
        let staged = try await NativeMusicImportFile.stage(zip, fileLimit: 100, archiveLimit: 1000)
        XCTAssertTrue(staged.isArchive); await staged.close()
    }
    @MainActor func testBatchContinuesAfterUnreadableFileAndDeduplicatesPickerSelection() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let good = try file(folder, "good.mp3"), missing = folder.appendingPathComponent("missing.mp3")
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        store.importMusicFiles([missing, good, good])
        XCTAssertEqual(store.musicImport?.index, 1); XCTAssertEqual(store.musicImport?.total, 2)
        store.importMusicFiles([good]) // Repeated tap must not create another task/session.
        try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.map(\.outcome), [.failed, .imported])
        XCTAssertEqual(api.requests.filter { $0.0 == "/api/mini/music/uploads" }.count, 1)
        XCTAssertEqual(store.tracks.count, 1); XCTAssertNotNil(store.error)
    }
    @MainActor func testInvalidOffsetCleansServerUploadAndStopsUntrustedBatch() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let first = try file(folder, "first.mp3"), second = try file(folder, "second.mp3")
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        api.handler = { _, method, _ in method == "PUT" ? ["ok": true, "offset": true] : nil }
        store.importMusicFiles([first, second]); try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.map(\.outcome), [.failed, .notAttempted])
        XCTAssertEqual(api.requests.filter { $0.1 == "DELETE" }.count, 1)
        XCTAssertFalse(api.requests.contains { $0.0.hasSuffix("/finish") })
        XCTAssertTrue(store.tracks.isEmpty)
    }
    @MainActor func testNetworkFailureAbortsRemainingFilesButPreservesPriorSuccess() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let files = try ["first.mp3", "second.mp3", "third.mp3"].map { try file(folder, $0) }
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        api.handler = { path, method, _ in if path.hasSuffix("fixture-2"), method == "PUT" { throw URLError(.networkConnectionLost) }; return nil }
        store.importMusicFiles(files); try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.map(\.outcome), [.imported, .failed, .notAttempted])
        XCTAssertEqual(store.tracks.map(\.id), [1]); XCTAssertEqual(api.requests.filter { $0.1 == "DELETE" }.count, 1)
        XCTAssertNotNil(store.error)
    }
    @MainActor func testCancellationAfterChunkAcknowledgementCleansPartialOnly() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let first = try file(folder, "first.mp3"), second = try file(folder, "second.mp3")
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        api.handler = { _, method, _ in if method == "PUT" { store.cancelMusicImport() }; return nil }
        store.importMusicFiles([first, second]); try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.map(\.outcome), [.cancelled, .notAttempted])
        XCTAssertEqual(api.requests.filter { $0.1 == "DELETE" }.count, 1)
        XCTAssertFalse(api.requests.contains { $0.0.hasSuffix("/finish") })
    }
    @MainActor func testCancellationDuringCommittedFinishKeepsReceiptAndTracks() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let first = try file(folder, "first.mp3"), second = try file(folder, "second.mp3")
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        api.handler = { path, _, _ in if path.hasSuffix("/finish") { store.cancelMusicImport() }; return nil }
        store.importMusicFiles([first, second]); try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.map(\.outcome), [.imported, .notAttempted])
        XCTAssertEqual(store.tracks.map(\.id), [1]); XCTAssertFalse(api.requests.contains { $0.1 == "DELETE" })
    }
    @MainActor func testZIPUsesAsyncFinishAndConsumesStatusReceipt() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let zip = try file(folder, "archive.zip")
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        api.handler = { path, method, body in
            if path.hasSuffix("/finish") { XCTAssertEqual(body?["async"] as? Bool, true); return ["ok": true, "status": "processing", "retry_after": 1] }
            if method == "GET", path.contains("/uploads/") { return ["ok": true, "status": "completed", "archive": true, "tracks": [["id": 42, "title": "ZIP fixture"]], "added": 1, "duplicates": 0, "skipped": 0] }
            return nil
        }
        store.importMusicFiles([zip]); try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.map(\.outcome), [.imported]); XCTAssertEqual(store.tracks.map(\.id), [42])
        XCTAssertFalse(api.requests.contains { $0.1 == "DELETE" })
    }
    @MainActor func testCancellingAcceptedZIPDoesNotDeleteLiveWorkerOrClaimRollback() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let zip = try file(folder, "archive.zip")
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        api.handler = { path, _, _ in
            if path.hasSuffix("/finish") { store.cancelMusicImport(); return ["ok": true, "status": "processing", "retry_after": 1] }
            return nil
        }
        store.importMusicFiles([zip]); try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.map(\.outcome), [.processing])
        XCTAssertFalse(api.requests.contains { $0.1 == "DELETE" })
        XCTAssertTrue(store.error?.contains("может продолжаться") == true)
    }
    @MainActor func testArchiveLimitIsSeparateAndOldServerFallsBackToAudioLimit() async {
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        await store.refresh()
        XCTAssertEqual(store.musicImportFileLimit, 128 * 1024 * 1024)
        XCTAssertEqual(store.musicImportArchiveLimit, 512 * 1024 * 1024)
        api.archiveLimit = nil; await store.refresh()
        XCTAssertEqual(store.musicImportArchiveLimit, store.musicImportFileLimit)
    }
    func testIntegerPolicyRejectsBooleanNegativeFractionalAndNonfiniteOffsets() {
        let values: [Any] = [true, -1, 1.25, Double.infinity, "12"]
        for value in values { XCTAssertNil(NativeMusicImportPolicy.integer(value)) }
        XCTAssertEqual(NativeMusicImportPolicy.integer(0), 0)
        XCTAssertEqual(NativeMusicImportPolicy.integer(512 * 1024), 512 * 1024)
    }
    @MainActor func testZIPReceiptReportsPartialErrorsAndAllBadArchiveIsNotSuccess() async throws {
        let folder = try directory(); defer { try? FileManager.default.removeItem(at: folder) }
        let zip = try file(folder, "archive.zip")
        let api = MusicImportOwnerFixture(), store = NativeStore(api: api, audio: AudioController()); defer { store.disconnect() }
        var values: [[String: Any]] = [["id": 42, "title": "Kept fixture"]]
        api.handler = { path, method, _ in
            if path.hasSuffix("/finish") || method == "GET" && path.contains("/uploads/") {
                return ["ok": true, "status": "completed", "archive": true, "tracks": values, "added": values.count, "duplicates": 0, "skipped": 1, "errors": ["Unsupported fixture"]]
            }
            return nil
        }
        store.importMusicFiles([zip]); try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.first?.outcome, .imported)
        XCTAssertTrue(store.musicImportResults.first?.message.contains("частично") == true)
        XCTAssertTrue(store.musicImportResults.first?.message.contains("ошибок: 1") == true)
        values = []; store.importMusicFiles([zip]); try await waitForImport(store)
        XCTAssertEqual(store.musicImportResults.first?.outcome, .failed)
        XCTAssertEqual(store.tracks.map(\.id), [42], "A bad later archive never removes prior successful music")
        XCTAssertFalse(api.requests.contains { $0.1 == "DELETE" }, "Completed receipts must never be rolled back")
    }
}
