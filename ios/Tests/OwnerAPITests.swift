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
    func testCatalogBudgetDoesNotChangeControlOrUploadDeadlines() {
        XCTAssertEqual(OwnerAPI.jsonRequestTimeout(path: "/api/mini/music/tracks/1/enrichment", method: "POST"), 25)
        XCTAssertEqual(OwnerAPI.jsonRequestTimeout(path: "/api/mini/music/tracks/1/enrichment/confirm", method: "POST"), 25)
        XCTAssertEqual(OwnerAPI.jsonRequestTimeout(path: "/api/mini/music/tracks/1/lyrics", method: "GET"), 25)
        for path in ["/api/mini/music/session", "/api/mini/music/transfers", "/api/mini/music/uploads/fixture/finish",
                     "/api/mini/music/tracks/1/enrichment/restore", "/api/mini/music/tracks/1/enrichment\n"] {
            XCTAssertEqual(OwnerAPI.jsonRequestTimeout(path: path, method: "POST"), 12)
        }
        XCTAssertEqual(OwnerAPI.jsonRequestTimeout(path: "/api/mini/music/tracks/1/enrichment", method: "GET"), 12)
    }
    func testLegacyFailedTransferIsAReceiptNotAnUnsupportedResponse() throws {
        let id = String(repeating: "a", count: 32)
        let receipt: [String: Any] = ["ok": false, "transfer_id": id, "status": "failed",
            "detail": "ПК не подтвердил запуск музыки", "session": ["device": "local", "state": "paused"]]
        let inner = try JSONSerialization.data(withJSONObject: receipt)
        let envelope = try JSONSerialization.data(withJSONObject: ["_s": 200, "_b": String(decoding: inner, as: UTF8.self)])
        for path in ["/api/mini/music/transfers", "/api/mini/music/transfers/" + id, "/api/mini/music/transfers/" + id + "/cancel"] {
            let value = try OwnerAPI.decode(envelope, status: 200, path: path)
            XCTAssertEqual(value["status"] as? String, "failed")
            XCTAssertEqual(value["transfer_id"] as? String, id)
            XCTAssertEqual(value["detail"] as? String, receipt["detail"] as? String)
        }
        for path in ["", "/api/mini/music/session", "/api/mini/music/transfers/" + String(repeating: "b", count: 32), "/api/mini/music/transfers/" + id + "/ack"] {
            XCTAssertThrowsError(try OwnerAPI.decode(envelope, status: 200, path: path))
        }
        for field in ["transfer_id", "status", "detail", "session"] {
            var malformed = receipt; malformed.removeValue(forKey: field)
            XCTAssertThrowsError(try OwnerAPI.decode(JSONSerialization.data(withJSONObject: malformed), status: 200, path: "/api/mini/music/transfers"))
        }
        for invalidID in [id + "\n", id + "\r", id + "/cancel", String(repeating: "g", count: 32)] {
            var malformed = receipt; malformed["transfer_id"] = invalidID
            XCTAssertThrowsError(try OwnerAPI.decode(JSONSerialization.data(withJSONObject: malformed), status: 200, path: "/api/mini/music/transfers"))
        }
        XCTAssertThrowsError(try OwnerAPI.decode(envelope, status: 401, path: "/api/mini/music/transfers"))
    }

    @MainActor func testActualProxyTransportPreservesFailedHandoffAndAllowsRetry() async throws {
        // The older tests replaced OwnerService and missed this decode boundary.
        for oldServer in [true, false] {
            OwnerHTTPFixture.requests = []
            let transferID = String(repeating: "c", count: 32)
            var fail = true
            var ownerSessionKey = ""
            OwnerHTTPFixture.reply = { request in
                let path = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value ?? ""
                var result: [String: Any] = ["ok": true]
                if path == "/api/mini/bootstrap" { result["sources"] = [] }
                else if path.contains("/music/library") { result["tracks"] = [["id": 1, "title": "Fixture", "duration": 99]]; result["playlists"] = [] }
                else if path == "/api/mini/music/players" { result["players"] = [] }
                else if path == "/api/mini/music/session" {
                    result["session"] = ["track_id": 1, "device": "local", "state": "paused", "position": 12, "session_key": String(repeating: "z", count: 32)]
                } else if path == "/api/mini/music/transfers" {
                    result = ["ok": !oldServer || !fail, "transfer_id": transferID, "status": fail ? "failed" : "ready",
                        "detail": fail ? "ПК не подтвердил запуск музыки" : "", "error_code": "target_start_failed",
                        "session": ["track_id": 1, "device": "agent:Fixture", "session_key": ownerSessionKey, "state": "playing", "position": 12]]
                }
                let data = try! JSONSerialization.data(withJSONObject: result)
                let envelope = try! JSONSerialization.data(withJSONObject: ["_s": 200, "_b": String(decoding: data, as: UTF8.self)])
                return (200, ["Content-Type": "application/json"], envelope)
            }
            let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [OwnerHTTPFixture.self]
            let api = OwnerAPI(origin: try ServerOrigin("https://native-api-fixture.invalid"), configuration: config,
                savedSession: { SavedSession(value: "fixture-session", expires: Date().addingTimeInterval(60)) })
            let store = NativeStore(api: api, audio: AudioController())
            ownerSessionKey = store.sessionKey
            defer { store.disconnect(); api.invalidate() }
            await store.refresh()
            do { try await store.pickRoute(device: "agent:Fixture"); XCTFail("Failed target must not become ready") }
            catch { XCTAssertEqual(error.localizedDescription, "ПК не подтвердил запуск музыки") }
            XCTAssertFalse(store.busy)
            XCTAssertEqual(store.selectedDevice, "local")
            XCTAssertTrue(store.showRoutePicker)
            fail = false
            try await store.pickRoute(device: "agent:Fixture")
            XCTAssertEqual(store.selectedDevice, "agent:Fixture")
            XCTAssertFalse(store.busy)
            XCTAssertFalse(store.showRoutePicker)
        }
    }

    func testDiagnosticMappingCannotExposeQueryOrUnknownErrorText() {
        XCTAssertEqual(OwnerAPI.diagnosticOperation("/api/mini/music/transfers?token=PRIVATE"), .musicTransfer)
        XCTAssertEqual(OwnerAPI.diagnosticOperation("/api/mini/agents/PRIVATE/files/upload?path=PRIVATE"), .upload)
        XCTAssertEqual(OwnerAPI.diagnosticOperation("/PRIVATE?secret=PRIVATE"), .other)
        XCTAssertEqual(OwnerAPI.diagnosticError(OwnerAPIError(status: 409, message: "PRIVATE")), .conflict)
        XCTAssertEqual(OwnerAPI.diagnosticError(URLError(.timedOut)), .timeout)
    }

    func testSearchEllipsisIsNotMistakenForPathTraversal() {
        XCTAssertTrue(OwnerAPI.allowsJSONPath("/api/mini/music/library?q=Mix..%20Tape"))
        XCTAssertTrue(OwnerAPI.allowsJSONPath("/api/mini/music/library?q=.."))
        for path in ["", "/api/mini/../admin", "/api/mini/%2e%2e/admin", "/api/mini/%5cadmin", "/api/mini/%0aadmin", "https://other.invalid/api/mini/library", "/api/native/enrollment/options?x=1"] {
            XCTAssertFalse(OwnerAPI.allowsJSONPath(path), path)
        }
    }
    func testUnsupportedResponseNamesOperationButNeverEchoesPrivateData() throws {
        let html = Data("<!DOCTYPE html><html>secret-cookie</html>".utf8)
        XCTAssertThrowsError(try OwnerAPI.decode(html, status: 200, path: "/api/mini/music/session?session_key=secret-key")) { error in
            XCTAssertTrue(error.localizedDescription.contains("Состояние плеера"))
            XCTAssertTrue(error.localizedDescription.contains("веб-страницу"))
            XCTAssertFalse(error.localizedDescription.contains("secret"))
            XCTAssertEqual((error as? OwnerAPIError)?.detail?["code"] as? String, "unsupported_response")
        }
        XCTAssertThrowsError(try OwnerAPI.decode(Data(#"{"players":[]}"#.utf8), status: 200, path: "/api/mini/music/players")) {
            XCTAssertTrue($0.localizedDescription.contains("Список устройств"))
        }
        let envelope = try JSONSerialization.data(withJSONObject: ["_s": 200, "_b": String(data: html, encoding: .utf8)!])
        XCTAssertThrowsError(try OwnerAPI.decode(envelope, status: 200, path: "/api/mini/music/library")) {
            XCTAssertTrue($0.localizedDescription.contains("веб-страницу"))
            XCTAssertFalse($0.localizedDescription.contains("secret"))
        }
    }
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
    @MainActor func testAuthenticatedBinaryUsesBoundedProxyAndRejectsErrorPages() async throws {
        OwnerHTTPFixture.requests = []
        let bytes = Data([0x89, 0x50, 0x4e, 0x47])
        OwnerHTTPFixture.reply = { _ in (200, ["Content-Type": "image/png"], bytes) }
        let origin = try ServerOrigin("https://native-api-fixture.invalid"), config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [OwnerHTTPFixture.self]
        let api = OwnerAPI(origin: origin, configuration: config, savedSession: { SavedSession(value: "fixture-session", expires: Date().addingTimeInterval(60)) })
        let actual = try await api.binary("/api/mini/agents/PC/assets/fixture-token")
        XCTAssertEqual(actual, bytes)
        let request = try XCTUnwrap(OwnerHTTPFixture.requests.last)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), "xass_pwa=fixture-session")
        XCTAssertTrue(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.contains(.init(name: "_binary", value: "1")) == true)
        OwnerHTTPFixture.reply = { _ in (200, ["Content-Type": "text/html"], Data("<html>Login</html>".utf8)) }
        do { _ = try await api.binary("/api/mini/media/1"); XCTFail("Accepted an HTML login page as media") } catch {}
        OwnerHTTPFixture.reply = { _ in (200, ["Content-Type": "image/png", "Content-Length": String(OwnerAPI.maxAssetBytes + 1)], bytes) }
        do { _ = try await api.binary("/api/mini/media/1"); XCTFail("Accepted oversized media") } catch {}
        api.invalidate()
    }
    func testBinaryTransportCannotBecomeAnArbitraryAuthenticatedDownloader() {
        for path in ["https://evil.invalid/media/1", "/api/mini/config", "/api/mini/media/0", "/api/mini/media/1?other=1", "/api/mini/agents/../assets/token", "/api/mini/agents/%2e%2e/assets/token", "/api/mini/agents/PC%2Fother/assets/token", "/api/mini/agents/PC%3Fquery/assets/token", "/api/mini/agents/PC%23fragment/assets/token", "/api/mini/agents/PC%0A/assets/token", "/api/mini/media/1#fragment"] {
            XCTAssertFalse(OwnerAPI.allowsBinaryPath(path), path)
        }
        XCTAssertTrue(OwnerAPI.allowsBinaryPath("/api/mini/media/12"))
        XCTAssertTrue(OwnerAPI.allowsBinaryPath("/api/mini/agents/" + OwnerAPI.pathComponent("Мой ПК") + "/assets/abc_123"))
    }
    @MainActor func testUploadPreservesRawEncryptedBodyAndDestinationThroughProxy() async throws {
        OwnerHTTPFixture.requests = []
        let payload = Data([88, 65, 83, 83, 1, 0, 255, 10])
        var received = Data()
        OwnerHTTPFixture.reply = { request in
            if let body = request.httpBody { received = body }
            else if let stream = request.httpBodyStream {
                stream.open(); defer { stream.close() }
                var buffer = [UInt8](repeating: 0, count: 1024)
                while stream.hasBytesAvailable {
                    let count = stream.read(&buffer, maxLength: buffer.count)
                    if count <= 0 { break }
                    received.append(contentsOf: buffer.prefix(count))
                }
            }
            return (200, ["Content-Type": "application/json"], Data(#"{"_s":200,"_b":"{\"ok\":true,\"command\":{\"id\":31}}"}"#.utf8))
        }
        let origin = try ServerOrigin("https://native-api-fixture.invalid"), config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [OwnerHTTPFixture.self]
        let api = OwnerAPI(origin: origin, configuration: config, savedSession: { SavedSession(value: "fixture-session", expires: Date().addingTimeInterval(60)) })
        defer { api.invalidate() }
        let path = "/api/mini/agents/PC%2B1/files/upload?root=xass_files&path=Folder%20%2B%20%23A&filename=Report%2B1.txt"
        let reply = try await api.upload(path, data: payload, headers: ["Content-Type": "application/x-xass-sealed", "X-XASS-Cipher": "xass-sealed-v1", "X-XASS-Inner-Type": "text/plain", "X-XASS-Action-Proof": "xna_fixture"])
        XCTAssertEqual((reply["command"] as? [String: Any])?["id"] as? Int, 31)
        XCTAssertEqual(received, payload)
        let request = try XCTUnwrap(OwnerHTTPFixture.requests.last)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value, path)
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-XASS-Cipher"), "xass-sealed-v1")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-XASS-Inner-Type"), "text/plain")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-XASS-Action-Proof"), "xna_fixture")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), "xass_pwa=fixture-session")
        do { _ = try await api.upload(path, data: payload, headers: ["Cookie": "untrusted"]); XCTFail("Arbitrary authenticated header accepted") } catch {}
        do { _ = try await api.upload(path, data: Data(repeating: 0, count: OwnerAPI.maxAssetBytes + 1), headers: [:]); XCTFail("Oversized upload accepted") } catch {}
        XCTAssertEqual(OwnerHTTPFixture.requests.count, 1)
    }
    func testUploadCannotTargetAnotherEndpointOrEscapeFileRoots() {
        let base = "/api/mini/agents/PC/files/upload"
        XCTAssertTrue(OwnerAPI.allowsUploadPath(base + "?root=downloads&path=&filename=file.txt"))
        XCTAssertTrue(OwnerAPI.allowsUploadPath("/api/mini/music/tracks/42/artwork"))
        XCTAssertTrue(OwnerAPI.isArtworkUploadPath("/api/mini/music/tracks/42/artwork"))
        for path in [
            "https://evil.invalid" + base + "?root=downloads&path=&filename=file.txt",
            "/api/mini/config?root=downloads&path=&filename=file.txt",
            base + "?root=system&path=&filename=file.txt",
            base + "?root=downloads&path=%2E%2E&filename=file.txt",
            base + "?root=downloads&path=&filename=folder%2Ffile.txt",
            base + "?root=downloads&path=&filename=file%0A.txt",
            base + "?root=downloads&path=&filename=file.txt&root=desktop",
            "/api/mini/music/tracks/0/artwork",
            "/api/mini/music/tracks/42/artwork?other=1",
            "/api/mini/music/tracks/42/../artwork"
        ] { XCTAssertFalse(OwnerAPI.allowsUploadPath(path), path) }
    }

    @MainActor func testArtworkUploadUsesOwnerOnlyPutContract() async throws {
        OwnerHTTPFixture.requests = []
        OwnerHTTPFixture.reply = { _ in
            (200, ["Content-Type": "application/json"],
             Data(#"{"_s":200,"_b":"{\"ok\":true,\"artwork\":{\"source\":\"owner\",\"content_type\":\"image/jpeg\"}}"}"#.utf8))
        }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [OwnerHTTPFixture.self]
        let api = OwnerAPI(origin: try ServerOrigin("https://native-api-fixture.invalid"), configuration: config,
                           savedSession: { SavedSession(value: "fixture-session", expires: Date().addingTimeInterval(60)) })
        defer { api.invalidate() }
        let data = Data([0xff, 0xd8, 0xff, 0xd9])
        _ = try await api.upload("/api/mini/music/tracks/42/artwork", data: data,
                                 headers: ["Content-Type": "image/jpeg"])
        let request = try XCTUnwrap(OwnerHTTPFixture.requests.last)
        XCTAssertEqual(request.httpMethod, "PUT")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "image/jpeg")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), "xass_pwa=fixture-session")
        do {
            _ = try await api.upload("/api/mini/music/tracks/42/artwork", data: data,
                                     headers: ["Content-Type": "text/html"])
            XCTFail("Non-image artwork upload accepted")
        } catch { }
        XCTAssertEqual(OwnerHTTPFixture.requests.count, 1)
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
