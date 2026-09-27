import XCTest
import Speech
@testable import XASS

final class NativeTranscriptTests: XCTestCase {
    private func word(_ text: String, _ time: Double, _ duration: Double = 0.2) -> NativeTranscriptWord {
        NativeTranscriptWord(text: text, time: time, duration: duration)
    }

    func testClipScheduleIsBoundedAndPreservesExactFinalDuration() throws {
        let clips = try XCTUnwrap(NativeTranscriptPolicy.clips(duration: 90.25))
        XCTAssertEqual(clips, [.init(start: 0, duration: 45), .init(start: 45, duration: 45), .init(start: 90, duration: 0.25)])
        XCTAssertEqual(NativeTranscriptPolicy.clips(duration: 900)?.count, 20)
        XCTAssertEqual(NativeTranscriptPolicy.clips(duration: 45)?.count, 1)
        for value in [Double.nan, Double.infinity, -Double.infinity, -1, 0, 900.001] {
            XCTAssertNil(NativeTranscriptPolicy.clips(duration: value))
        }
    }

    func testRecognitionRequestExplicitlyRequiresOnDeviceWithoutPartialPublication() {
        let request = NativeTranscriptPolicy.recognitionRequest(file: URL(fileURLWithPath: "/fixture/segment.m4a"))
        XCTAssertTrue(request.requiresOnDeviceRecognition)
        XCTAssertFalse(request.shouldReportPartialResults)
        XCTAssertEqual(request.taskHint, .dictation)
        // No recognizer or authorization request is started by this test.
    }

    func testLRCUsesRealCentisecondsAndStableChronologicalOrder() {
        let result = NativeTranscriptFormat.lrc([
            word("Later", 45.127), word("First", 0), word("Same", 0), word("After minute", 61.239)
        ])
        XCTAssertEqual(result, "[00:00.00] First Same\n[00:45.13] Later\n[01:01.24] After minute")
    }

    func testLRCGroupingBreaksOnLongPauseSevenWordsAndLongPhrase() {
        let phrase = (0..<8).map { word("w\($0)", Double($0) * 0.1) }
        XCTAssertEqual(NativeTranscriptFormat.lrc(phrase), "[00:00.00] w0 w1 w2 w3 w4 w5 w6\n[00:00.70] w7")
        XCTAssertEqual(NativeTranscriptFormat.lrc([word("One", 0), word("Two", 1.3)]), "[00:00.00] One\n[00:01.30] Two")
        XCTAssertEqual(NativeTranscriptFormat.lrc([word("One", 0, 5), word("Two", 4.01)]), "[00:00.00] One\n[00:04.01] Two")
    }

    func testLRCRejectsInvalidNumbersAndPreventsTimestampInjection() {
        let result = NativeTranscriptFormat.lrc([
            word("\r\n  Actual\twords [99:00.00]\u{0000}", 1.234),
            word("NaN", .nan), word("Infinite", .infinity), word("Negative", -1),
            word("Out of range", 900), word("Bad duration", 2, .nan), word("Huge duration", 3, 1e50),
            word("   ", 4), word("Negative duration", 5, -1)
        ])
        XCTAssertEqual(result, "[00:01.23] Actual words (99:00.00)")
        XCTAssertFalse(result.contains("\u{0000}")); XCTAssertFalse(result.contains("\r"))
    }

    func testOversizedTranscriptsFailWithoutSavingSilentTruncation() {
        XCTAssertEqual(NativeTranscriptFormat.lrc(Array(repeating: word("Test", 0), count: 8001)), "")
        let tooLarge = (0..<1000).map { word(String(repeating: "я", count: 100), Double($0) * 0.2) }
        XCTAssertEqual(NativeTranscriptFormat.lrc(tooLarge), "")
    }

    func testSourceLinksAreRestrictedToHTTPSCatalogsWithoutUserInfoOrPorts() {
        for value in ["https://lrclib.net/", "https://musicbrainz.org/recording/test", "https://coverartarchive.org/release/test"] {
            XCTAssertNotNil(NativeEnrichmentPresentation.sourceURL(value), value)
        }
        let rejected: [Any] = [
            123, "http://lrclib.net/", "file:///secret", "javascript:alert(1)",
            "https://evil.example/", "https://lrclib.net.evil.example/", "https://evil.lrclib.net/",
            "https://user:pass@lrclib.net/", "https://@lrclib.net/", "https://lrclib.net:443/",
            "https://lrclib.net/" + String(repeating: "a", count: 1000)
        ]
        for value in rejected { XCTAssertNil(NativeEnrichmentPresentation.sourceURL(value)) }
        XCTAssertNil(NativeEnrichmentPresentation.sourceURL(nil))
    }

    func testLocalFilePolicyRejectsMissingDirectoryRemoteAndSymlinkWithoutMutatingSource() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("xass-transcript-test-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: folder) }
        let source = folder.appendingPathComponent("fixture.audio"), alias = folder.appendingPathComponent("link.audio")
        let bytes = Data([0, 1, 2, 3])
        try bytes.write(to: source)
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: source)
        XCTAssertTrue(NativeTranscriptPolicy.localFile(source))
        XCTAssertFalse(NativeTranscriptPolicy.localFile(alias))
        XCTAssertFalse(NativeTranscriptPolicy.localFile(folder))
        XCTAssertFalse(NativeTranscriptPolicy.localFile(folder.appendingPathComponent("missing")))
        XCTAssertFalse(NativeTranscriptPolicy.localFile(URL(string: "https://example.invalid/audio.mp3")!))
        XCTAssertFalse(NativeTranscriptPolicy.localFile(URL(string: "file://remote.invalid/audio.mp3")!))
        XCTAssertEqual(try Data(contentsOf: source), bytes)
    }

    @MainActor func testLatePreviousCallbackCannotCompleteNextRecognitionAndEachResumesOnce() async throws {
        let gate = NativeTranscriptCompletion<Int>(), old = UUID(), next = UUID()
        var cleanups = 0
        let first: Int = try await withCheckedThrowingContinuation { done in
            XCTAssertTrue(gate.install(done, token: old))
            XCTAssertEqual(gate.activeToken, old)
            XCTAssertTrue(gate.resolve(.success(1), token: old) { cleanups += 1 })
            XCTAssertFalse(gate.resolve(.success(2), token: old) { cleanups += 1 })
        }
        XCTAssertEqual(first, 1)
        let second: Int = try await withCheckedThrowingContinuation { done in
            XCTAssertTrue(gate.install(done, token: next))
            XCTAssertFalse(gate.resolve(.failure(CancellationError()), token: old) { cleanups += 1 })
            XCTAssertEqual(gate.activeToken, next)
            XCTAssertTrue(gate.resolve(.success(3), token: next) { cleanups += 1 })
        }
        XCTAssertEqual(second, 3)
        XCTAssertEqual(cleanups, 2)
        XCTAssertNil(gate.activeToken)
    }

    @MainActor func testCancellationWinsOverLateSuccess() async throws {
        let gate = NativeTranscriptCompletion<Int>(), token = UUID()
        do {
            let _: Int = try await withCheckedThrowingContinuation { done in
                XCTAssertTrue(gate.install(done, token: token))
                XCTAssertTrue(gate.resolve(.failure(CancellationError()), token: token))
                XCTAssertFalse(gate.resolve(.success(99), token: token))
            }
            XCTFail("Cancelled request must not publish a result")
        } catch { XCTAssertTrue(error is CancellationError) }
    }

    func testExportCancellationBeforeStartNeverStartsAndAfterStartCancelsExactlyOnce() {
        let before = NativeTranscriptExportGate()
        var starts = 0, cancels = 0
        before.cancel { cancels += 1 }
        XCTAssertFalse(before.start { starts += 1 })
        XCTAssertEqual(starts, 0); XCTAssertEqual(cancels, 0)
        let after = NativeTranscriptExportGate()
        XCTAssertTrue(after.start { starts += 1 })
        XCTAssertFalse(after.start { starts += 1 })
        after.cancel { cancels += 1 }; after.cancel { cancels += 1 }
        XCTAssertEqual(starts, 1); XCTAssertEqual(cancels, 1)
    }
}
