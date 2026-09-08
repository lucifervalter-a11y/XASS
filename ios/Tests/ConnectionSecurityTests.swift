import XCTest
@testable import XASS

final class ConnectionSecurityTests: XCTestCase {
    func testOriginIsCanonicalAndPortIsPinned() throws {
        let origin = try ServerOrigin(" https://XASS.example:443/miniapp.php?standalone=1 ")
        XCTAssertEqual(origin.url.absoluteString, "https://xass.example")
        XCTAssertEqual(origin.entryURL.absoluteString, "https://xass.example/miniapp.php?standalone=1")
        XCTAssertTrue(origin.contains(URL(string: "https://xass.example/anything")!))
        XCTAssertFalse(origin.contains(URL(string: "http://xass.example/anything")!))
        XCTAssertFalse(origin.contains(URL(string: "https://xass.example:444/anything")!))
        XCTAssertFalse(origin.contains(URL(string: "https://xass.example.attacker.invalid/anything")!))
        XCTAssertFalse(origin.contains(URL(string: "https://user@xass.example/anything")!))
        XCTAssertNotEqual(origin.namespace, try ServerOrigin("https://other.example").namespace)
    }
    func testInvalidOriginsRejected() {
        for text in ["", "http://xass.example", "javascript:alert(1)", "https://user:secret@xass.example", "https://@xass.example", "https://xass.example:99999", "https://xass.example:0", "https://xass.example\\@evil.invalid", "https://xass.example/\nsecret"] {
            XCTAssertThrowsError(try ServerOrigin(text), text)
        }
    }
    func testPairKeyStaysInFragmentAndOtherOriginRejected() throws {
        let origin = try ServerOrigin("https://xass.example")
        let token = "xpw_" + String(repeating: "a", count: 43)
        let url = try origin.loginURL(pairInput: token)
        XCTAssertEqual(url.fragment, "pair=" + token)
        XCTAssertFalse(url.query?.contains(token) ?? false)
        XCTAssertEqual(try origin.loginURL(pairInput: url.absoluteString), url)
        XCTAssertThrowsError(try origin.loginURL(pairInput: "https://other.example/miniapp.php#pair=" + token))
        XCTAssertThrowsError(try origin.loginURL(pairInput: "xpw_short"))
    }
    func testMediaAllowsOnlyExactTrackTicketAndOrigin() throws {
        let origin = try ServerOrigin("https://xass.example")
        let direct = "/api/music/tracks/7/stream?ticket=abc.def"
        XCTAssertEqual(try origin.mediaURL(direct, trackID: 7).host, "xass.example")
        var proxy = URLComponents(string: "https://xass.example/proxy.php")!
        proxy.queryItems = [.init(name: "_binary", value: "1"), .init(name: "_media", value: "1"), .init(name: "_p", value: direct)]
        XCTAssertNoThrow(try origin.mediaURL(proxy.url!.absoluteString, trackID: 7))
        for text in ["https://other.example" + direct, "http://xass.example" + direct, "/api/music/tracks/8/stream?ticket=abc", "/api/mini/music/library", direct + "&ticket=second", direct + "#fragment", "file:///tmp/music", "", "//other.example" + direct, "/proxy.php?_p=" + direct] {
            XCTAssertThrowsError(try origin.mediaURL(text, trackID: 7), text)
        }
    }
    func testCommandBoundsAndKnownActions() throws {
        XCTAssertEqual(try NativeAudioCommand(["action": "play", "trackId": 7, "position": 1.5, "volume": 80]).trackID, 7)
        let invalid: [[String: Any]] = [["action": "eval"], ["action": "play"], ["action": "play", "trackId": -1], ["action": "play", "trackId": 1.5], ["action": "volume", "volume": 101], ["action": "seek", "position": -1], ["action": "seek", "position": Double.infinity]]
        for body in invalid {
            XCTAssertThrowsError(try NativeAudioCommand(body))
        }
        XCTAssertNoThrow(try NativeAudioCommand(["action": "downloads"]))
    }
    func testRangeParsingAndTelegramLoginAllowlist() {
        XCTAssertEqual(SecureMediaLoader.parseRange("bytes 100-199/500")?.start, 100)
        XCTAssertEqual(SecureMediaLoader.parseRange("bytes 100-199/500")?.total, 500)
        XCTAssertNil(SecureMediaLoader.parseRange("bytes */500"))
        XCTAssertNil(SecureMediaLoader.parseRange("bytes 100-199/0"))
    }
    @MainActor func testTelegramPopupDoesNotAuthorizeLookalikes() {
        XCTAssertTrue(WebController.telegramLogin(URL(string: "https://oauth.telegram.org/auth")!))
        XCTAssertFalse(WebController.telegramLogin(URL(string: "https://oauth.telegram.org.attacker.invalid/auth")!))
        XCTAssertFalse(WebController.telegramLogin(URL(string: "https://user@oauth.telegram.org/auth")!))
    }
    func testNativeQueueAdvancesWithoutJSAndRepeatModesAreExplicit() throws {
        let tracks: [[String: Any]] = [["trackId": 1], ["trackId": 2], ["trackId": 3]]
        let off = try NativePlaybackQueue(tracks)
        XCTAssertEqual(off.next(currentID: 1, direction: 1, automatic: true)?.id, 2)
        XCTAssertNil(off.next(currentID: 3, direction: 1, automatic: true))
        XCTAssertEqual(off.next(currentID: 3, direction: -1, automatic: false)?.id, 2)
        let all = try NativePlaybackQueue(tracks, repeatMode: "all")
        XCTAssertEqual(all.next(currentID: 3, direction: 1, automatic: true)?.id, 1)
        let one = try NativePlaybackQueue(tracks, repeatMode: "one")
        XCTAssertEqual(one.next(currentID: 2, direction: 1, automatic: true)?.id, 2)
        XCTAssertEqual(one.next(currentID: 2, direction: 1, automatic: false)?.id, 3)
        XCTAssertThrowsError(try NativePlaybackQueue([["trackId": 1], ["trackId": 1]]))
        XCTAssertThrowsError(try NativePlaybackQueue(tracks, repeatMode: "unknown"))
        XCTAssertThrowsError(try NativePlaybackQueue(Array(repeating: ["trackId": 1], count: 201)))
    }
    func testRealPHPEnvelopeDecodedWithoutTreatingHTTP200AsAuthorized() throws {
        let body = Data(#"{"ok":true,"path":"/api/music/tracks/7/stream?ticket=abc"}"#.utf8)
        let wrapped = try JSONSerialization.data(withJSONObject: ["_s": 200, "_b": String(data: body, encoding: .utf8)!])
        XCTAssertEqual(try ServerEnvelope.decode(wrapped, httpStatus: 200)["path"] as? String, "/api/music/tracks/7/stream?ticket=abc")
        XCTAssertEqual(try ServerEnvelope.decode(body, httpStatus: 200)["ok"] as? Bool, true)
        let denied = try JSONSerialization.data(withJSONObject: ["_s": 401, "_b": String(data: body, encoding: .utf8)!])
        XCTAssertThrowsError(try ServerEnvelope.decode(denied, httpStatus: 200))
        XCTAssertThrowsError(try ServerEnvelope.decode(wrapped, httpStatus: 403))
        XCTAssertThrowsError(try ServerEnvelope.decode(Data(#"{"_s":200,"_b":"invalid"}"#.utf8), httpStatus: 200))
    }
    func testFullCyrillicQueueFitsBoundedBridge() throws {
        let items = (1...200).map { ["trackId": $0, "title": String(repeating: "Я", count: 240), "artist": String(repeating: "Ж", count: 240)] as [String: Any] }
        XCTAssertNoThrow(try NativeAudioCommand(["action": "play", "trackId": 1, "queue": items]))
    }
}
