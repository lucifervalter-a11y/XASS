import XCTest
import CryptoKit
@testable import XASS

/// Fixed vectors generated independently with Python cryptography using the exact
/// browser export format and pc_client/e2e_crypto.py HKDF/AAD/packet contract.
final class NativeWorkspaceCryptoTests: XCTestCase {
    private let password = "correct horse battery staple"
    private let envelope = #"{"format":"xass-e2e-owner-key","version":1,"kdf":"PBKDF2-SHA256","iterations":210000,"cipher":"AES-256-GCM","salt":"AAECAwQFBgcICQoLDA0ODw","nonce":"AAECAwQFBgcICQoL","data":"JM3ozsWkQ_nLY0OsN0FOAbf0iwJ2RxouRIEwP9PK5TLyDSBlkyWbzZibr8eMqQDCQbwpKTc44GH1EMwV5Nds3kv8SVyLrvUDOkkqQ0vSRI3PEJzz3CespsNJynZkmORAjZ8nPZ32WbyXxSAcIJCLjMRDM8-WanA_lvPoel6n78ixI_hUz_m0lMC_SL-UiKoLedGPLzNBlAEkgTyX2CaYI3SoSpUlYVtDuYyHPVhZoUVPeu6Ve9im8O67G_ZPyvQvAHyUuYepNwkJggGecso8onjAFQ8prs-0iP90iwKhnABTEzGqF8aq6YAGpejbKDxv9NpC4gCBEYdg7dFI1M0ohlU38hI7Ng5MeTOdxouwMWH1Ntbxr_pP0OtFM9kC7L2MbExG1-AW96qSNF2yGIAiAhKeoviItozI39P9JymqqkU8ey9XpToHEJRxAIXW7idCo6b7Df5dwnZQ_w"}"#
    private let peer: [String: Any] = ["kty": "EC", "crv": "P-256", "x": "4TgAvq9qfe3k3d-HTkrcwnG_Bp6M_Cxe-YkIS-wzsmA", "y": "FlckNJo3faNuNXp5z3M80kzhKYG9mhlnWwix027VaAE"]
    private let sealed = "WEFTUwEAAQIDBAUGBwgJCgslAPYC8I4RPe-wOLD-WjFf4Rs1XElcnDNXySUB1_aP8KjITXh4my0idv3t-fB-tbEY0QJftlLL0EW9hfzQFw"
    private let privateBytes = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdbzRU"

    func testBrowserKeyExportImportsAndPCClipboardDecrypts() throws {
        let key = try NativeWorkspaceCrypto.importData(Data(envelope.utf8), password: password)
        XCTAssertEqual(NativeWorkspaceCrypto.encoded(key), privateBytes)
        let blob = try NativeWorkspaceCrypto.bytes(sealed)
        let plain = try NativeWorkspaceCrypto.open(blob, privateKey: key, peer: peer, purpose: "clipboard")
        XCTAssertEqual(String(data: plain, encoding: .utf8), "Тест: защищённый буфер 🎵")
    }
    func testWrongPasswordTamperedPacketAndWrongPurposeAreRejected() throws {
        XCTAssertThrowsError(try NativeWorkspaceCrypto.importData(Data(envelope.utf8), password: "incorrect password"))
        let key = try NativeWorkspaceCrypto.bytes(privateBytes, count: 32)
        var blob = try NativeWorkspaceCrypto.bytes(sealed)
        XCTAssertThrowsError(try NativeWorkspaceCrypto.open(blob, privateKey: key, peer: peer, purpose: "screenshot"))
        blob[blob.count - 1] ^= 1
        XCTAssertThrowsError(try NativeWorkspaceCrypto.open(blob, privateKey: key, peer: peer, purpose: "clipboard"))
    }
    func testNativeClipboardEncryptionMatchesIndependentPythonVector() throws {
        let key = try NativeWorkspaceCrypto.bytes(privateBytes, count: 32)
        let nonce = try AES.GCM.Nonce(data: Data((0..<12).map(UInt8.init)))
        let blob = try NativeWorkspaceCrypto.seal(Data("Тест: защищённый буфер 🎵".utf8), privateKey: key, peer: peer, purpose: "clipboard", nonce: nonce)
        XCTAssertEqual(NativeWorkspaceCrypto.encoded(blob), sealed)
    }
    func testImportRejectsUnboundedKDFAndMismatchedPublicKey() throws {
        var value = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(envelope.utf8)) as? [String: Any])
        value["iterations"] = 999999999
        XCTAssertThrowsError(try NativeWorkspaceCrypto.importData(JSONSerialization.data(withJSONObject: value), password: password))
        let other = P256.KeyAgreement.PrivateKey()
        let point = other.publicKey.x963Representation
        let publicJwk: [String: Any] = ["kty": "EC", "crv": "P-256", "x": NativeWorkspaceCrypto.encoded(point[1..<33]), "y": NativeWorkspaceCrypto.encoded(point[33..<65])]
        var secret = publicJwk; secret["d"] = privateBytes
        XCTAssertThrowsError(try NativeWorkspaceCrypto.validatePair(["privateJwk": secret, "publicJwk": publicJwk]))
    }
    func testNonCanonicalBase64AndTruncatedPacketsAreRejected() throws {
        XCTAssertThrowsError(try NativeWorkspaceCrypto.bytes("AAAA="))
        XCTAssertThrowsError(try NativeWorkspaceCrypto.bytes("AB"))
        let key = try NativeWorkspaceCrypto.bytes(privateBytes, count: 32)
        XCTAssertThrowsError(try NativeWorkspaceCrypto.open(Data([88,65,83,83,1]), privateKey: key, peer: peer, purpose: "clipboard"))
    }
}
