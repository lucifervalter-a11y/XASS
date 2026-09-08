import XCTest
@testable import XASS

private final class OwnerHTTPFixture: URLProtocol {
    static var reply: (URLRequest) -> (Int, [String: String], Data) = { _ in (200, [:], Data(#"{"ok":true}"#.utf8)) }
    static var requests: [URLRequest] = []
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "native-api-fixture.invalid" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests.append(request)
        let result = Self.reply(request)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: result.0, httpVersion: "HTTP/1.1", headerFields: result.1)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: result.2); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

final class OwnerAPITests: XCTestCase {
    func testPHPEnvelopeAllowsFullLibraryAndPreservesActualFailure() throws {
        let body = try JSONSerialization.data(withJSONObject: ["ok": true, "title": String(repeating: "Я", count: 40000)])
        let envelope = try JSONSerialization.data(withJSONObject: ["_s": 200, "_b": String(data: body, encoding: .utf8)!])
        XCTAssertEqual(try OwnerAPI.decode(envelope, status: 200)["title"] as? String, String(repeating: "Я", count: 40000))
        let failed = try JSONSerialization.data(withJSONObject: ["_s": 503, "_b": #"{"detail":"Технические работы"}"#])
        XCTAssertThrowsError(try OwnerAPI.decode(failed, status: 200)) { error in
            XCTAssertEqual((error as? OwnerAPIError)?.status, 503); XCTAssertEqual(error.localizedDescription, "Технические работы")
        }
        XCTAssertThrowsError(try OwnerAPI.decode(envelope, status: 401)) { XCTAssertEqual(($0 as? OwnerAPIError)?.status, 401) }
        XCTAssertThrowsError(try OwnerAPI.decode(Data(repeating: 0, count: OwnerAPI.maxResponseBytes + 1), status: 200))
        XCTAssertThrowsError(try OwnerAPI.decode(Data(#"{"ok":false}"#.utf8), status: 200))
    }
    @MainActor func testCookieIsNativeOnlyAndExactProxyPathIsUsed() async throws {
        OwnerHTTPFixture.requests = []
        OwnerHTTPFixture.reply = { _ in (200, ["Content-Type": "application/json"], Data(#"{"ok":true,"tracks":[]}"#.utf8)) }
        let origin = try ServerOrigin("https://native-api-fixture.invalid"), config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [OwnerHTTPFixture.self]
        let api = OwnerAPI(origin: origin, configuration: config, savedSession: { SavedSession(value: "fixture-session", expires: Date().addingTimeInterval(60)) }, saveSession: { _ in })
        let body = try await api.request("/api/mini/music/library")
        XCTAssertEqual(body["ok"] as? Bool, true)
        let request = try XCTUnwrap(OwnerHTTPFixture.requests.first)
        XCTAssertEqual(request.url?.host, origin.host); XCTAssertEqual(request.url?.path, "/proxy.php")
        XCTAssertEqual(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value, "/api/mini/music/library")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), "xass_pwa=fixture-session")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Origin"), origin.url.absoluteString)
        XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalCacheData)
        api.invalidate()
    }
    @MainActor func testExpiredSessionCannotReadButFreshPairEnrollmentCanSetHttpOnlyCookie() async throws {
        OwnerHTTPFixture.requests = []
        OwnerHTTPFixture.reply = { _ in (200, ["Content-Type": "application/json", "Set-Cookie": "xass_pwa=enrolled-fixture; Path=/; Secure; HttpOnly; Max-Age=120"], Data(#"{"ok":true,"device_id":"fixture-device"}"#.utf8)) }
        let origin = try ServerOrigin("https://native-api-fixture.invalid"), config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [OwnerHTTPFixture.self]; var saved: SavedSession?
        let api = OwnerAPI(origin: origin, configuration: config, savedSession: { nil }, saveSession: { saved = $0 })
        do { _ = try await api.request("/api/mini/music/library"); XCTFail("Missing owner cookie was accepted") }
        catch { XCTAssertEqual((error as? OwnerAPIError)?.status, 401) }
        XCTAssertTrue(OwnerHTTPFixture.requests.isEmpty)
        _ = try await api.request("/api/native/enrollment/verify", method: "POST", body: ["pair_token": "fixture-not-a-real-pair"])
        XCTAssertNil(OwnerHTTPFixture.requests.first?.value(forHTTPHeaderField: "Cookie"))
        XCTAssertEqual(saved?.value, "enrolled-fixture"); XCTAssertNotNil(saved?.expires)
        do { _ = try await api.request("https://evil.invalid/api/mini/music/library"); XCTFail("Arbitrary URL accepted") } catch {}
        do { _ = try await api.request("/api/native/unknown", method: "POST"); XCTFail("Unknown native route accepted") } catch {}
        api.invalidate()
    }
    func testNativeSigningEncodingDoesNotPrehashOrAcceptForeignPairLink() throws {
        let bytes = Data("XASS native challenge Я".utf8)
        XCTAssertEqual(try NativeEncoding.decode(NativeEncoding.base64URL(bytes)), bytes)
        XCTAssertThrowsError(try NativeEncoding.decode("%2Fsecret"))
        let origin = try ServerOrigin("https://xass.example"), token = "xpw_" + String(repeating: "x", count: 43)
        XCTAssertEqual(try NativeEncoding.pairToken("https://xass.example/miniapp.php#pair=" + token, origin: origin), token)
        XCTAssertThrowsError(try NativeEncoding.pairToken("https://other.example/miniapp.php#pair=" + token, origin: origin))
    }
    func testNativeModelsRejectMissingIdentityAndBoundQueueAroundCurrentTrack() throws {
        XCTAssertNil(LibraryTrack(["title": "Missing ID"]))
        XCTAssertNil(NativeDevice(["id": 1, "source_type": "SERVER", "source_name": "Server"]))
        let tracks = (1...350).compactMap { LibraryTrack(["id": $0, "title": "Трек \($0)"]) }
        let queue = NativeValue.queue(tracks, currentID: 280)
        XCTAssertEqual(queue.count, 200); XCTAssertTrue(queue.contains { $0["trackId"] as? Int == 280 })
        XCTAssertEqual(NativeValue.time(.infinity), "0:00"); XCTAssertEqual(NativeValue.time(62), "1:02")
        XCTAssertEqual(LibraryTrack(["id": 1, "mime": "audio/mp4"])?.pcSupported, false)
    }
}
