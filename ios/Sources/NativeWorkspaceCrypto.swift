import Foundation
import CryptoKit
import CommonCrypto

/// Compatible with assets/xass-e2e.js and pc_client/e2e_crypto.py.
/// The original owner key is imported, never regenerated during migration.
enum NativeWorkspaceCrypto {
    static let missingKey = OwnerAPIError(status: 0, message: "Импортируйте файл ключа XASS с устройства, на котором привязывали этот ПК.")
    static func keyName(_ origin: ServerOrigin) -> String { "workspace-e2e-" + origin.namespace }
    static func hasKey(_ origin: ServerOrigin) -> Bool { SecureStore.load(keyName(origin)) != nil }
    static func encoded(_ data: Data) -> String { NativeEncoding.base64URL(data) }
    static func bytes(_ value: Any?, count: Int? = nil, limit: Int = 16384) throws -> Data {
        guard let text = value as? String, !text.isEmpty, text.utf8.count <= limit * 2,
              text.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil,
              let data = Data(base64Encoded: text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/") + String(repeating: "=", count: (4 - text.count % 4) % 4)),
              data.count <= limit, count == nil || data.count == count, encoded(data) == text else { throw OwnerAPIError.invalidResponse }
        return data
    }
    static func publicKey(_ jwk: [String: Any]) throws -> P256.KeyAgreement.PublicKey {
        guard jwk["kty"] as? String == "EC", jwk["crv"] as? String == "P-256" else { throw OwnerAPIError.invalidResponse }
        let point = try Data([4]) + bytes(jwk["x"], count: 32) + bytes(jwk["y"], count: 32)
        return try P256.KeyAgreement.PublicKey(x963Representation: point)
    }
    static func validatePair(_ value: [String: Any]) throws -> Data {
        guard let secret = value["privateJwk"] as? [String: Any], let published = value["publicJwk"] as? [String: Any] else { throw OwnerAPIError.invalidResponse }
        let data = try bytes(secret["d"], count: 32)
        let key = try P256.KeyAgreement.PrivateKey(rawRepresentation: data)
        let actual = key.publicKey.x963Representation
        guard try publicKey(secret).x963Representation == actual,
              try publicKey(published).x963Representation == actual else { throw OwnerAPIError.invalidResponse }
        return data
    }
    /// Expensive password derivation runs away from the UI; callers save only a fully verified key.
    static func importData(_ data: Data, password: String) throws -> Data {
        guard data.count <= 16 * 1024, password.unicodeScalars.count >= 12, password.utf16.count <= 4096,
              let envelope = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              envelope["format"] as? String == "xass-e2e-owner-key", envelope["version"] as? Int == 1,
              envelope["kdf"] as? String == "PBKDF2-SHA256", envelope["iterations"] as? Int == 210000,
              envelope["cipher"] as? String == "AES-256-GCM" else {
            throw OwnerAPIError(status: 0, message: "Нужен файл ключа XASS до 16 КБ и его пароль (от 12 символов).")
        }
        let salt = try bytes(envelope["salt"], count: 16), nonce = try bytes(envelope["nonce"], count: 12)
        let ciphertext = try bytes(envelope["data"], limit: 8192)
        guard ciphertext.count >= 17 else { throw OwnerAPIError.invalidResponse }
        var keyData = Data(count: 32)
        let passwordData = Data(password.utf8)
        let status = keyData.withUnsafeMutableBytes { key in
            passwordData.withUnsafeBytes { pass in
                salt.withUnsafeBytes { saltBytes in
                    CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2), pass.bindMemory(to: Int8.self).baseAddress, passwordData.count,
                        saltBytes.bindMemory(to: UInt8.self).baseAddress, salt.count, CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA256),
                        210000, key.bindMemory(to: UInt8.self).baseAddress, 32)
                }
            }
        }
        guard status == kCCSuccess else { throw OwnerAPIError.invalidResponse }
        do {
            let box = try AES.GCM.SealedBox(nonce: AES.GCM.Nonce(data: nonce), ciphertext: ciphertext.dropLast(16), tag: ciphertext.suffix(16))
            let plain = try AES.GCM.open(box, using: SymmetricKey(data: keyData), authenticating: Data("xass-owner-key-export-v1".utf8))
            guard let keys = try JSONSerialization.jsonObject(with: plain) as? [String: Any] else { throw OwnerAPIError.invalidResponse }
            return try validatePair(keys)
        } catch {
            throw OwnerAPIError(status: 0, message: "Неверный пароль или повреждённый файл ключа. Сохранённый ключ не изменён.")
        }
    }
    static func isSealed(_ data: Data) -> Bool { data.count >= 5 && data.prefix(5) == Data([88, 65, 83, 83, 1]) }
    static func open(_ data: Data, privateKey: Data, peer: [String: Any], purpose: String) throws -> Data {
        guard isSealed(data), data.count >= 33 else { throw OwnerAPIError.invalidResponse }
        let key = try P256.KeyAgreement.PrivateKey(rawRepresentation: privateKey)
        let secret = try key.sharedSecretFromKeyAgreement(with: publicKey(peer))
        let aes = secret.hkdfDerivedSymmetricKey(using: SHA256.self, salt: Data("xass-e2e-v1".utf8), sharedInfo: Data("xass-e2e-aes".utf8), outputByteCount: 32)
        let box = try AES.GCM.SealedBox(nonce: AES.GCM.Nonce(data: data[5..<17]), ciphertext: data[17..<(data.count - 16)], tag: data.suffix(16))
        return try AES.GCM.open(box, using: aes, authenticating: Data(purpose.utf8))
    }
    static func open(_ data: Data, origin: ServerOrigin, peer: [String: Any], purpose: String) throws -> Data {
        guard let secret = SecureStore.load(keyName(origin)) else { throw missingKey }
        do { return try open(data, privateKey: secret, peer: peer, purpose: purpose) }
        catch { throw OwnerAPIError(status: 0, message: "Ключ не подходит к этому ПК или пакет повреждён. Импортируйте исходный ключ и повторите запрос.") }
    }
}
