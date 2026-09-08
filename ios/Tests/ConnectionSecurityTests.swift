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
        for body: [String: Any] in [["action": "eval"], ["action": "play"], ["action": "play", "trackId": -1], ["action": "play", "trackId": 1.5], ["action": "volume", "volume": 101], ["action": "seek", "position": -1], ["action": "seek", "position": Double.infinity]] {
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
}
