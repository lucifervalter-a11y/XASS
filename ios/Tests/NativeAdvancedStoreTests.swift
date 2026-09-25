import XCTest
import CryptoKit
@testable import XASS

@MainActor private final class AdvancedOwnerFixture: OwnerService {
    let origin = try! ServerOrigin("https://advanced-fixture.invalid")
    var requests: [(String, String, [String: Any]?)] = []
    var handler: ((String, String, [String: Any]?) throws -> [String: Any])?
    var uploadHandler: ((String, Data, [String: String]) throws -> [String: Any])?
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method, body))
        guard let handler else { throw OwnerAPIError.invalidResponse }
        return try handler(path, method, body)
    }
    func upload(_ path: String, data: Data, headers: [String: String]) async throws -> [String: Any] {
        guard let uploadHandler else { throw OwnerAPIError.invalidResponse }
        return try uploadHandler(path, data, headers)
    }
}

final class NativeAdvancedStoreTests: XCTestCase {
    @MainActor func testCommandMatchesNewIDAndRejectsOldSuccess() async throws {
        let api = AdvancedOwnerFixture(), owner = NativeStore(api: api, audio: AudioController())
        defer { owner.disconnect() }
        let data = NativeAdvancedStore(owner: owner)
        let device = try XCTUnwrap(NativeDevice(["id": 1, "source_type": "PC_AGENT", "source_name": "PC + #1"]))
        api.handler = { path, method, _ in
            if method == "POST" { return ["ok": true, "command": ["id": 8]] }
            XCTAssertTrue(path.contains("PC%20%2B%20%231"))
            return ["ok": true, "commands": [
                ["id": 7, "status": "completed", "result": ["ok": true, "details": ["text": "OLD"]]],
                ["id": 8, "status": "failed", "result": ["ok": false, "message": "New request failed"]]
            ]]
        }
        do { try await data.getClipboard(device); XCTFail("Must reject the current failed command") }
        catch { XCTAssertEqual(error.localizedDescription, "New request failed") }
        XCTAssertEqual(data.clipboard, "")
        let previousRequests = api.requests.count
        do { _ = try await data.command(device, name: "shutdown"); XCTFail("Advanced command path must not bypass protected operations") }
        catch {}
        XCTAssertEqual(api.requests.count, previousRequests)
    }
    @MainActor func testArchiveKeepsNegativeChatIDAndMergesEarlierPages() async throws {
        let api = AdvancedOwnerFixture(), owner = NativeStore(api: api, audio: AudioController())
        defer { owner.disconnect() }
        let data = NativeAdvancedStore(owner: owner)
        api.handler = { path, method, _ in
            XCTAssertEqual(method, "GET"); XCTAssertTrue(path.hasPrefix("/api/mini/conversations/-1000123456789?"))
            XCTAssertTrue(path.contains("deleted_only=true"))
            let earlier = path.contains("before=5")
            return ["ok": true, "has_more": !earlier, "messages": earlier ? [["id": 3, "text": "earlier"], ["id": 5, "text": "overlap"]] : [["id": 5, "text": "fifth"], ["id": 6, "text": "sixth"]]]
        }
        try await data.loadMessages(chat: -1000123456789, filter: "deleted")
        XCTAssertTrue(data.hasMore)
        try await data.loadMessages(chat: -1000123456789, filter: "deleted", earlier: true)
        XCTAssertEqual(data.messages.map(\.id), [3, 5, 6])
        XCTAssertFalse(data.hasMore)
        XCTAssertEqual(NativeConversation(["chat_id": NSNumber(value: Int64(-1000123456789))])?.id, -1000123456789)
    }
    @MainActor func testFilterQueryDoesNotInjectOrLoseLiteralPlus() throws {
        let path = NativeAdvancedStore.query("/api/mini/timeline", [.init(name: "device", value: "PC + &level=critical#")])
        XCTAssertTrue(path.contains("%2B")); XCTAssertTrue(path.contains("%26")); XCTAssertTrue(path.contains("%23"))
        let url = try XCTUnwrap(URLComponents(string: "https://example.invalid" + path))
        XCTAssertEqual(url.queryItems?.count, 1)
        XCTAssertEqual(url.queryItems?.first?.value, "PC + &level=critical#")
    }
    func testScenarioBindingIncludesReviewedActionsTargetsAndDelay() {
        let scenario = NativeScenario(["id": "night", "name": "Night", "actions": ["quiet_on", "lock_all"], "devices": ["PC 1"], "delay_sec": 30])
        XCTAssertTrue(scenario.dangerous)
        XCTAssertEqual(scenario.binding["scenario_id"] as? String, "night")
        XCTAssertEqual(scenario.binding["actions"] as? [String], ["quiet_on", "lock_all"])
        XCTAssertEqual(scenario.binding["devices"] as? [String], ["PC 1"])
        XCTAssertEqual(scenario.binding["delay_sec"] as? Int, 30)
    }
    func testRemoteFileNamesCannotEscapeTheirFolder() {
        for name in ["..", ".", "../file", "folder/file", "folder\\file", "C:secret", "\0"] { XCTAssertNil(NativeRemoteFile(["name": name, "type": "file"])) }
        XCTAssertNotNil(NativeRemoteFile(["name": "Отчёт 🎵.txt", "type": "file", "size": 10]))
    }
    @MainActor func testFailedRuleSavePreservesConfirmedRules() async throws {
        let api = AdvancedOwnerFixture(), owner = NativeStore(api: api, audio: AudioController())
        defer { owner.disconnect() }
        let data = NativeAdvancedStore(owner: owner)
        api.handler = { _, method, _ in
            if method == "GET" { return ["ok": true, "rules": [["id": "first", "name": "Original", "condition": "cpu_high", "threshold": 90]]] }
            throw OwnerAPIError(status: 503, message: "Unavailable")
        }
        try await data.loadRules()
        var draft = try XCTUnwrap(data.rules.first); draft.name = "Unsaved"
        await data.perform { try await data.saveRule(draft) }
        XCTAssertEqual(data.rules.first?.name, "Original"); XCTAssertNotNil(data.error); XCTAssertFalse(data.busy)
    }
    @MainActor func testFileDeletionBindsReviewedFolderAndCannotBypassEnrollment() async throws {
        let api = AdvancedOwnerFixture(), owner = NativeStore(api: api, audio: AudioController())
        defer { owner.disconnect() }
        owner.authorization.identity.forget()
        let data = NativeAdvancedStore(owner: owner)
        let device = try XCTUnwrap(NativeDevice(["id": 7, "source_type": "PC_AGENT", "source_name": "PC 1"]))
        let file = try XCTUnwrap(NativeRemoteFile(["name": "Отчёт +1.txt", "type": "file", "size": 12]))
        let target = try XCTUnwrap(NativeRemoteFileTarget(root: "documents", folder: "Документы/Личные", file: file))
        XCTAssertEqual(target.payload as? [String: String], ["root": "documents", "path": "Документы/Личные/Отчёт +1.txt"])
        XCTAssertEqual(target.binding(device: device)["source_id"] as? Int, 7)
        XCTAssertEqual(target.binding(device: device)["command"] as? String, "file_delete")
        XCTAssertEqual(target.binding(device: device)["payload"] as? [String: String], target.payload as? [String: String])
        do { try await data.deleteFile(device, target: target); XCTFail("Deletion requires a registered hardware key") }
        catch { XCTAssertEqual((error as? OwnerAPIError)?.status, 428) }
        XCTAssertFalse(owner.busy); XCTAssertTrue(api.requests.isEmpty)
        XCTAssertNil(NativeRemoteFileTarget(root: "documents", folder: "../private", file: file))
        XCTAssertNil(NativeRemoteFileTarget(root: "system", folder: "", file: file))
    }
    @MainActor func testFileUploadEncryptsBytesAndPreservesLiteralFilenameAndDestination() async throws {
        let api = AdvancedOwnerFixture(), owner = NativeStore(api: api, audio: AudioController())
        let data = NativeAdvancedStore(owner: owner)
        let device = try XCTUnwrap(NativeDevice(["id": 7, "source_type": "PC_AGENT", "source_name": "PC +1"]))
        let ownerKey = P256.KeyAgreement.PrivateKey(), agentKey = P256.KeyAgreement.PrivateKey()
        func jwk(_ key: P256.KeyAgreement.PublicKey) -> [String: Any] {
            let point = key.x963Representation
            return ["kty": "EC", "crv": "P-256", "x": NativeWorkspaceCrypto.encoded(point[1..<33]), "y": NativeWorkspaceCrypto.encoded(point[33..<65])]
        }
        let keyName = NativeWorkspaceCrypto.keyName(api.origin)
        try SecureStore.save(ownerKey.rawRepresentation, name: keyName)
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent("Отчёт + #1.txt"), plain = Data("Файл с iPhone 🎵".utf8)
        try plain.write(to: url)
        defer { SecureStore.remove(keyName); try? FileManager.default.removeItem(at: folder); owner.disconnect() }
        var uploadCount = 0, listing = false
        api.uploadHandler = { path, bytes, headers in
            uploadCount += 1
            let parts = try XCTUnwrap(URLComponents(string: "https://example.invalid" + path))
            XCTAssertEqual(parts.path, "/api/mini/agents/PC +1/files/upload")
            let query = Dictionary(uniqueKeysWithValues: (parts.queryItems ?? []).map { ($0.name, $0.value ?? "") })
            XCTAssertEqual(query, ["root": "documents", "path": "Папка + #A", "filename": "Отчёт + #1.txt"])
            XCTAssertTrue(path.contains("%2B"))
            XCTAssertEqual(headers["Content-Type"], "application/x-xass-sealed")
            XCTAssertEqual(headers["X-XASS-Cipher"], "xass-sealed-v1")
            XCTAssertEqual(headers["X-XASS-Inner-Type"], "text/plain")
            XCTAssertNotEqual(bytes, plain)
            XCTAssertEqual(try NativeWorkspaceCrypto.open(bytes, privateKey: agentKey.rawRepresentation, peer: jwk(ownerKey.publicKey), purpose: "file_upload"), plain)
            return ["ok": true, "command": ["id": 19]]
        }
        api.handler = { path, method, body in
            if path == "/api/mini/bootstrap" { return ["ok": true, "sources": [["id": 7, "source_name": "PC +1", "last_payload": ["e2e_public_jwk": jwk(agentKey.publicKey)]]]] }
            if method == "POST" {
                XCTAssertEqual(body?["command"] as? String, "files_list"); listing = true
                return ["ok": true, "command": ["id": 20]]
            }
            let details: [String: Any] = listing ? ["root": "documents", "path": "Папка + #A", "entries": []] : ["filename": "Отчёт + #1.txt"]
            return ["ok": true, "commands": [["id": listing ? 20 : 19, "status": "completed", "result": ["ok": true, "details": details]]]]
        }
        try await data.uploadFile(device, url: url, root: "documents", path: "Папка + #A")
        XCTAssertEqual(uploadCount, 1); XCTAssertEqual(data.currentPath, "Папка + #A")
        SecureStore.remove(keyName)
        do { try await data.uploadFile(device, url: url, root: "documents", path: "Папка + #A"); XCTFail("A missing owner key must not cause plaintext fallback") }
        catch {}
        XCTAssertEqual(uploadCount, 1)
    }
}
