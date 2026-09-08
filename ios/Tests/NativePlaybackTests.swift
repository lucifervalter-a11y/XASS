import XCTest
import AVFoundation
import Combine
@testable import XASS

private func silenceWAV(seconds: Double = 0.35) -> Data {
    let sampleRate: UInt32 = 8000, bytes = UInt32(seconds * 8000) * 2
    var value = Data()
    func text(_ string: String) { value.append(Data(string.utf8)) }
    func u16(_ number: UInt16) { var number = number.littleEndian; withUnsafeBytes(of: &number) { value.append(contentsOf: $0) } }
    func u32(_ number: UInt32) { var number = number.littleEndian; withUnsafeBytes(of: &number) { value.append(contentsOf: $0) } }
    text("RIFF"); u32(bytes + 36); text("WAVEfmt "); u32(16); u16(1); u16(1)
    u32(sampleRate); u32(sampleRate * 2); u16(2); u16(16); text("data"); u32(bytes)
    value.append(Data(repeating: 0, count: Int(bytes)))
    return value
}

private final class AudioHTTPFixture: URLProtocol {
    static let bytes = silenceWAV(seconds: 1)
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "audio-fixture.invalid" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let range = (request.value(forHTTPHeaderField: "Range") ?? "bytes=0-").replacingOccurrences(of: "bytes=", with: "").split(separator: "-", omittingEmptySubsequences: false)
        let start = max(0, Int(range.first ?? "0") ?? 0), total = Self.bytes.count
        let end = min(total - 1, range.count > 1 ? Int(range[1]) ?? total - 1 : total - 1)
        guard start <= end else {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 416, httpVersion: "HTTP/1.1", headerFields: ["Content-Range": "bytes */\(total)"])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocolDidFinishLoading(self); return
        }
        let body = Self.bytes.subdata(in: start..<(end + 1))
        let headers = ["Content-Type": "audio/wav", "Content-Length": "\(body.count)", "Content-Range": "bytes \(start)-\(end)/\(total)", "Accept-Ranges": "bytes"]
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 206, httpVersion: "HTTP/1.1", headerFields: headers)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

final class NativePlaybackTests: XCTestCase {
    @MainActor func testRealAVPlayerLoadsThroughBoundedRangeLoader() async throws {
        let origin = try ServerOrigin("https://audio-fixture.invalid")
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [AudioHTTPFixture.self]
        let loader = SecureMediaLoader(origin: origin, trackID: 7,
            url: try origin.mediaURL("/api/music/tracks/7/stream?ticket=test", trackID: 7), configuration: configuration)
        let item = AVPlayerItem(asset: loader.asset()), player = AVPlayer()
        let ready = expectation(description: "AVPlayer recognizes WAV served by custom range loader")
        var fulfilled = false
        let observer = item.observe(\.status, options: [.new]) { item, _ in
            if !fulfilled && item.status != .unknown { fulfilled = true; ready.fulfill() }
        }
        player.replaceCurrentItem(with: item); player.play()
        await fulfillment(of: [ready], timeout: 15)
        XCTAssertEqual(item.status, .readyToPlay, item.error?.localizedDescription ?? "")
        player.pause(); player.replaceCurrentItem(with: nil); observer.invalidate(); loader.invalidate()
    }
    @MainActor func testOfflineAutoNextUsesNativeQueueWithoutAnyWebView() async throws {
        let origin = try ServerOrigin("https://offline-\(UUID().uuidString.lowercased()).invalid")
        let library = try OfflineLibrary(origin: origin)
        let tracks = [DownloadedTrack(id: 101, title: "First", artist: "Fixture", duration: 0.35, fileExtension: "wav"), DownloadedTrack(id: 102, title: "Second", artist: "Fixture", duration: 0.35, fileExtension: "wav")]
        for track in tracks { try silenceWAV().write(to: library.file(for: track), options: .atomic) }
        try library.save(tracks)
        defer { try? FileManager.default.removeItem(at: library.directory) }
        let playable = try await AVURLAsset(url: library.file(for: tracks[0])).load(.isPlayable)
        XCTAssertTrue(playable, "Generated local WAV must be recognized by AVFoundation before testing the queue")
        let audio = AudioController(); audio.configure(origin)
        let second = expectation(description: "Native end event advances to second local track")
        var fulfilled = false
        let observer = audio.$trackID.sink { id in if id == 102 && !fulfilled { fulfilled = true; second.fulfill() } }
        audio.playOffline(tracks[0])
        await fulfillment(of: [second], timeout: 15)
        XCTAssertEqual(audio.trackID, 102, "state=\(audio.state) position=\(audio.position) duration=\(audio.duration) error=\(audio.error ?? "none")")
        audio.stop(); observer.cancel()
    }
}
