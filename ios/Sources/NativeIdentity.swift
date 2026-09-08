import Foundation
import Security
import LocalAuthentication

struct NativeKeyRecord: Codable {
    var publicKey: String
    var deviceID: String?
}

enum NativeEncoding {
    static func base64URL(_ data: Data) -> String { data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "") }
    static func decode(_ value: String) throws -> Data {
        guard value.count <= 16384, value.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil,
              let data = Data(base64Encoded: value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/") + String(repeating: "=", count: (4 - value.count % 4) % 4)) else { throw OwnerAPIError.invalidResponse }
        return data
    }
    static func pairToken(_ input: String, origin: ServerOrigin) throws -> String {
        let url = try origin.loginURL(pairInput: input)
        guard let fragment = url.fragment, fragment.hasPrefix("pair=") else { throw XASSErr.invalidPairLink }
        return String(fragment.dropFirst(5))
    }
}

/// The private key is not exportable. Every use requires iOS user presence;
/// a browser cookie alone cannot create a server-authorized native device.
final class NativeIdentity {
    let origin: ServerOrigin
    private var tag: Data { Data(("app.xass.native." + origin.namespace).utf8) }
    private var recordName: String { "native-device-" + origin.namespace }
    init(origin: ServerOrigin) { self.origin = origin }
    var record: NativeKeyRecord? {
        guard let data = SecureStore.load(recordName) else { return nil }
        return try? JSONDecoder().decode(NativeKeyRecord.self, from: data)
    }
    var enrolled: Bool { record?.deviceID != nil }

    func prepareKey() throws -> NativeKeyRecord {
        if let record = record { return record }
        var failure: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly,
                [.privateKeyUsage, .userPresence], &failure) else { throw XASSErr.keychain }
        let attributes: [String: Any] = [kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeySizeInBits as String: 256, kSecAttrTokenID as String: kSecAttrTokenIDSecureEnclave,
            kSecPrivateKeyAttrs as String: [kSecAttrIsPermanent as String: true, kSecAttrApplicationTag as String: tag, kSecAttrAccessControl as String: access]]
        guard let key = SecKeyCreateRandomKey(attributes as CFDictionary, &failure), let publicKey = SecKeyCopyPublicKey(key),
              let bytes = SecKeyCopyExternalRepresentation(publicKey, &failure) as Data?, bytes.count == 65, bytes.first == 4 else {
            throw OwnerAPIError(status: 0, message: "Не удалось создать защищённый ключ iPhone. Включите код-пароль устройства. Симулятор не поддерживает регистрацию Secure Enclave.")
        }
        let value = NativeKeyRecord(publicKey: NativeEncoding.base64URL(bytes), deviceID: nil)
        try SecureStore.save(JSONEncoder().encode(value), name: recordName)
        return value
    }
    func saveDeviceID(_ id: String) throws {
        guard !id.isEmpty, id.count <= 160, var value = record else { throw OwnerAPIError.invalidResponse }
        value.deviceID = id; try SecureStore.save(JSONEncoder().encode(value), name: recordName)
    }
    func sign(_ message: String, reason: String) async throws -> String {
        let data = try NativeEncoding.decode(message), tag = self.tag
        guard !data.isEmpty, data.count <= 8192 else { throw OwnerAPIError.invalidResponse }
        return try await withCheckedThrowingContinuation { completion in
            DispatchQueue.global(qos: .userInitiated).async {
                let context = LAContext(); context.localizedReason = reason; context.localizedCancelTitle = "Отмена"
                let query: [String: Any] = [kSecClass as String: kSecClassKey, kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
                    kSecAttrApplicationTag as String: tag, kSecReturnRef as String: true, kSecUseAuthenticationContext as String: context,
                    kSecUseOperationPrompt as String: reason]
                var result: CFTypeRef?
                guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let result = result else {
                    completion.resume(throwing: OwnerAPIError(status: 0, message: "Подтверждение отменено или ключ iPhone недоступен. Действие не отправлено.")); return
                }
                let key = result as! SecKey
                var failure: Unmanaged<CFError>?
                guard SecKeyIsAlgorithmSupported(key, .sign, .ecdsaSignatureMessageX962SHA256),
                      let signature = SecKeyCreateSignature(key, .ecdsaSignatureMessageX962SHA256, data as CFData, &failure) as Data? else {
                    completion.resume(throwing: OwnerAPIError(status: 0, message: "Не удалось подтвердить действие. Повторите Face ID или код-пароль.")); return
                }
                completion.resume(returning: NativeEncoding.base64URL(signature))
            }
        }
    }
    func forget() {
        SecItemDelete([kSecClass as String: kSecClassKey, kSecAttrApplicationTag as String: tag] as CFDictionary)
        SecureStore.remove(recordName)
    }
}

@MainActor final class NativeActionAuthorization {
    let api: OwnerService
    let identity: NativeIdentity
    init(api: OwnerService) { self.api = api; identity = NativeIdentity(origin: api.origin) }
    func enroll(pairInput: String, deviceName: String) async throws {
        let token = try NativeEncoding.pairToken(pairInput, origin: api.origin), key = try identity.prepareKey()
        let options = try await api.request("/api/native/enrollment/options", method: "POST", body: ["public_key": key.publicKey, "pair_token": token])
        guard let challenge = options["challenge_id"] as? String, let message = options["message"] as? String else { throw OwnerAPIError.invalidResponse }
        let signature = try await identity.sign(message, reason: "Разрешить этому iPhone управление вашим XASS")
        try Task.checkCancellation()
        let verified = try await api.request("/api/native/enrollment/verify", method: "POST", body: ["challenge_id": challenge, "public_key": key.publicKey, "signature": signature, "pair_token": token, "device_name": String(deviceName.prefix(120))])
        guard let id = verified["device_id"] as? String else { throw OwnerAPIError.invalidResponse }
        try identity.saveDeviceID(id)
    }
    func proof(purpose: String, binding: [String: Any], reason: String) async throws -> String {
        guard let id = identity.record?.deviceID else { throw OwnerAPIError(status: 428, message: "Для команд ПК привяжите защищённый ключ iPhone одноразовой ссылкой из Telegram.") }
        let options = try await api.request("/api/native/actions/options", method: "POST", body: ["device_id": id, "purpose": purpose, "binding": binding])
        guard let challenge = options["challenge_id"] as? String, let message = options["message"] as? String else { throw OwnerAPIError.invalidResponse }
        let signature = try await identity.sign(message, reason: reason)
        try Task.checkCancellation()
        let verified = try await api.request("/api/native/actions/verify", method: "POST", body: ["device_id": id, "challenge_id": challenge, "signature": signature])
        guard let proof = verified["action_proof"] as? String, proof.hasPrefix("xna_") else { throw OwnerAPIError.invalidResponse }
        return proof
    }
}
