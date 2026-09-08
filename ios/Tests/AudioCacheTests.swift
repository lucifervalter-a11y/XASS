import XCTest
@testable import XASS

final class AudioCacheTests: XCTestCase {
    private func fixture() throws -> (URL, ServerOrigin, AudioCache) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("XASS-cache-test-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let origin = try ServerOrigin("https://cache-" + UUID().uuidString.lowercased() + ".invalid")
        return (root, origin, try AudioCache(origin: origin, root: root))
    }
    private func sparse(_ url: URL, bytes: UInt64) throws {
        XCTAssertTrue(FileManager.default.createFile(atPath: url.path, contents: Data()))
        let handle = try FileHandle(forWritingTo: url); defer { try? handle.close() }
        try handle.truncate(atOffset: bytes)
    }
    func testSecondActualPlayIsRequiredAndPolicyPersists() async throws {
        let (root, origin, cache) = try fixture(); defer { try? FileManager.default.removeItem(at: root) }
        _ = try await cache.load()
        let first = try await cache.recordPlay(7), second = try await cache.recordPlay(7)
        XCTAssertFalse(first); XCTAssertTrue(second)
        _ = try await cache.setLimit(512)
        let restored = try AudioCache(origin: origin, root: root)
        let snapshot = try await restored.load()
        XCTAssertEqual(snapshot.limitMB, 512)
        _ = try await restored.setLimit(0)
        let disabled = try await restored.recordPlay(7)
        XCTAssertFalse(disabled)
    }
    func testLRUEvictsOldAutomaticFileAndNeverPinnedLibrary() async throws {
        let (root, origin, cache) = try fixture(); defer { try? FileManager.default.removeItem(at: root) }
        _ = try await cache.load()
        let manual = try OfflineLibrary(origin: origin)
        defer { try? FileManager.default.removeItem(at: manual.directory) }
        let pinned = DownloadedTrack(id: 1, title: "Pinned", artist: "Fixture", duration: 1, fileExtension: "wav")
        try Data([1, 2, 3]).write(to: manual.file(for: pinned)); try manual.save([pinned])
        let a = DownloadedTrack(id: 1, title: "Older", artist: "Fixture", duration: 1, fileExtension: "wav")
        let b = DownloadedTrack(id: 2, title: "Newer", artist: "Fixture", duration: 1, fileExtension: "wav")
        try sparse(cache.file(1, suffix: "wav"), bytes: 150 * 1024 * 1024)
        _ = try await cache.insert(a, at: Date(timeIntervalSince1970: 1))
        try sparse(cache.file(2, suffix: "wav"), bytes: 150 * 1024 * 1024)
        let snapshot = try await cache.insert(b, at: Date(timeIntervalSince1970: 2))
        XCTAssertEqual(snapshot.entries.map(\.id), [2]); XCTAssertEqual(snapshot.bytes, 150 * 1024 * 1024)
        XCTAssertFalse(FileManager.default.fileExists(atPath: cache.file(1, suffix: "wav").path))
        XCTAssertEqual(try Data(contentsOf: manual.file(for: pinned)), Data([1, 2, 3]))
        _ = try await cache.clear()
        XCTAssertEqual(manual.tracks().map(\.id), [1])
    }
    func testOriginIsolationOrphanCleanupAndPromotion() async throws {
        let (root, origin, cache) = try fixture(); defer { try? FileManager.default.removeItem(at: root) }
        _ = try await cache.load()
        let other = try AudioCache(origin: ServerOrigin("https://other-cache.invalid"), root: root)
        _ = try await other.load()
        XCTAssertNotEqual(cache.directory, other.directory)
        let track = DownloadedTrack(id: 3, title: "Cached", artist: "Fixture", duration: 1, fileExtension: "wav")
        try Data([4, 5, 6]).write(to: cache.file(3, suffix: "wav"))
        _ = try await cache.insert(track)
        let library = try OfflineLibrary(origin: origin); defer { try? FileManager.default.removeItem(at: library.directory) }
        let cached = await cache.snapshot()
        let copied = try await cache.copyToPinned(cached.entries[0], to: library)
        XCTAssertTrue(library.tracks().isEmpty, "Cache actor must not mutate the manual index")
        try library.save([copied])
        XCTAssertEqual(library.tracks().map(\.id), [3])
        try Data([0]).write(to: cache.file(999, suffix: "wav"))
        let reopened = try AudioCache(origin: origin, root: root)
        _ = try await reopened.load()
        XCTAssertFalse(FileManager.default.fileExists(atPath: cache.file(999, suffix: "wav").path))
        _ = try await reopened.clear()
        XCTAssertEqual(try Data(contentsOf: library.file(for: track)), Data([4, 5, 6]))
        let isolated = await other.snapshot(); XCTAssertEqual(isolated.bytes, 0)
    }
}
