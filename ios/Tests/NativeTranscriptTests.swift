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
        XCTAssertEqual(result, "[00:00.00] First Same\n[00:00.20]\n[00:45.13] Later\n[00:45.33]\n[01:01.24] After minute\n[01:01.44]")
    }

    func testExplicitAppleNetworkRequestKeepsFinalOnlyRecognition() {
        let request = NativeTranscriptPolicy.recognitionRequest(file: URL(fileURLWithPath: "/fixture/segment.m4a"), onDevice: false)
        XCTAssertFalse(request.requiresOnDeviceRecognition)
        XCTAssertFalse(request.shouldReportPartialResults)
    }

    func testFailedMiddleClipProducesPartialResultWithExactGapAndOriginalTimings() throws {
        var transcript = NativeTranscriptAccumulator(totalClips: 3, mode: .onDevice)
        try transcript.append(.success([word("First", 1)]), for: .init(start: 0, duration: 45))
        try transcript.append(.failure(OwnerAPIError(status: 408, message: "timeout")), for: .init(start: 45, duration: 45))
        try transcript.append(.success([word("Last", 2)]), for: .init(start: 90, duration: 10))
        let result = try transcript.result()
        XCTAssertTrue(result.isPartial)
        XCTAssertEqual(result.totalClips, 3)
        XCTAssertEqual(result.failedClips, [.init(start: 45, duration: 45)])
        XCTAssertTrue(result.lrc.contains("[00:01.00] First"))
        XCTAssertTrue(result.lrc.contains("[01:32.00] Last"))
        XCTAssertTrue(result.reviewNotice.contains("Частичная расшифровка"))
        XCTAssertTrue(result.reviewNotice.contains("1 из 3"))
        XCTAssertTrue(result.reviewNotice.contains("00:45–01:30"))
    }

    func testSuccessfulSilentClipIsNotReportedAsFailure() throws {
        var transcript = NativeTranscriptAccumulator(totalClips: 2, mode: .onDevice)
        try transcript.append(.success([]), for: .init(start: 0, duration: 45))
        try transcript.append(.success([word("Voice", 0)]), for: .init(start: 45, duration: 2))
        let result = try transcript.result()
        XCTAssertFalse(result.isPartial)
        XCTAssertEqual(result.failedClips, [])
        XCTAssertFalse(result.reviewNotice.contains("Пропуски"))
        XCTAssertTrue(result.reviewNotice.contains("на iPhone"))
    }

    func testAppleNetworkPartialResultDoesNotClaimAudioStayedOnDevice() throws {
        var transcript = NativeTranscriptAccumulator(totalClips: 2, mode: .appleNetwork)
        try transcript.append(.failure(OwnerAPIError(status: 422, message: "no speech")), for: .init(start: 0, duration: 45))
        try transcript.append(.success([word("Voice", 0)]), for: .init(start: 45, duration: 2))
        let result = try transcript.result()
        XCTAssertEqual(result.mode, .appleNetwork)
        XCTAssertTrue(result.isPartial)
        XCTAssertTrue(result.reviewNotice.contains("Аудио могло отправляться в Apple"))
        XCTAssertFalse(result.reviewNotice.contains("Аудио не отправлялось"))
    }

    func testAllFailedClipsThrowModeSpecificErrorInsteadOfPublishingEmptyResult() throws {
        for mode in [NativeTranscriptMode.onDevice, .appleNetwork] {
            var transcript = NativeTranscriptAccumulator(totalClips: 1, mode: mode)
            try transcript.append(.failure(OwnerAPIError(status: 422, message: "recognition failed")), for: .init(start: 0, duration: 45))
            XCTAssertThrowsError(try transcript.result()) { error in
                XCTAssertEqual(error.localizedDescription, mode.failureMessage)
                if mode == .appleNetwork {
                    XCTAssertTrue(error.localizedDescription.contains("через интернет"))
                    XCTAssertFalse(error.localizedDescription.contains("Аудио не отправлялось"))
                } else {
                    XCTAssertTrue(error.localizedDescription.contains("локальной модели"))
                }
            }
        }
    }

    func testCancellationCannotBecomeAPartialResult() throws {
        var transcript = NativeTranscriptAccumulator(totalClips: 2, mode: .onDevice)
        try transcript.append(.success([word("First", 0)]), for: .init(start: 0, duration: 45))
        XCTAssertThrowsError(try transcript.append(.failure(CancellationError()), for: .init(start: 45, duration: 1))) {
            XCTAssertTrue($0 is CancellationError)
        }
        XCTAssertThrowsError(try transcript.result()) { XCTAssertTrue($0 is CancellationError) }
    }

    func testNoWordsCannotBePublishedAsCompletedTranscript() throws {
        var transcript = NativeTranscriptAccumulator(totalClips: 1, mode: .appleNetwork)
        try transcript.append(.success([]), for: .init(start: 0, duration: 45))
        XCTAssertThrowsError(try transcript.result()) {
            XCTAssertTrue($0.localizedDescription.contains("Аудио могло отправляться в Apple"))
            XCTAssertFalse($0.localizedDescription.contains("Аудио не отправлялось"))
        }
    }

    func testAccumulatedWordLimitAndClipBoundsAreStillEnforced() throws {
        var transcript = NativeTranscriptAccumulator(totalClips: 2, mode: .onDevice)
        try transcript.append(.success([word("negative", -1), word("outside", 45), word("invalid", .nan)] + Array(repeating: word("valid", 0), count: 8000)), for: .init(start: 0, duration: 45))
        XCTAssertThrowsError(try transcript.append(.success([word("overflow", 0)]), for: .init(start: 45, duration: 1)))
        XCTAssertThrowsError(try transcript.result())
    }

    func testLRCGroupingBreaksOnLongPauseSevenWordsAndLongPhrase() {
        let phrase = (0..<8).map { word("w\($0)", Double($0) * 0.1) }
        XCTAssertEqual(NativeTranscriptFormat.lrc(phrase), "[00:00.00] w0 w1 w2 w3 w4 w5 w6\n[00:00.70] w7\n[00:00.90]")
        XCTAssertEqual(NativeTranscriptFormat.lrc([word("One", 0), word("Two", 1.3)]), "[00:00.00] One\n[00:00.20]\n[00:01.30] Two\n[00:01.50]")
        XCTAssertEqual(NativeTranscriptFormat.lrc([word("One", 0, 5), word("Two", 4.01)]), "[00:00.00] One\n[00:04.01] Two\n[00:05.00]")
    }

    func testLRCRejectsInvalidNumbersAndPreventsTimestampInjection() {
        let result = NativeTranscriptFormat.lrc([
            word("\r\n  Actual\twords [99:00.00]\u{0000}", 1.234),
            word("NaN", .nan), word("Infinite", .infinity), word("Negative", -1),
            word("Out of range", 900), word("Bad duration", 2, .nan), word("Huge duration", 3, 1e50),
            word("   ", 4), word("Negative duration", 5, -1)
        ])
        XCTAssertEqual(result, "[00:01.23] Actual words (99:00.00)\n[00:01.43]")
        XCTAssertFalse(result.contains("\u{0000}")); XCTAssertFalse(result.contains("\r"))
    }

    func testMeasuredPauseAndFinalWordEndStopLyricsHighlighting() {
        let text = NativeTranscriptFormat.lrc([word("First", 0, 0.6), word("Next phrase", 4, 0.8)])
        XCTAssertEqual(text, "[00:00.00] First\n[00:00.60]\n[00:04.00] Next phrase\n[00:04.80]")
        let lyrics = NativeLyrics(["lyrics": ["synced": true, "source": "on_device_transcription", "text": text, "lines": [
            ["time": 0.0, "text": "First"], ["time": 0.6, "text": ""],
            ["time": 4.0, "text": "Next phrase"], ["time": 4.8, "text": ""]
        ]]])
        XCTAssertNotNil(lyrics.activeLine(at: 0.5))
        XCTAssertNil(lyrics.activeLine(at: 2))
        XCTAssertNotNil(lyrics.activeLine(at: 4.5))
        XCTAssertNil(lyrics.activeLine(at: 15))
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
