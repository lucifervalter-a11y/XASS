import Foundation
import Security
import CryptoKit

enum XASSErr: LocalizedError {
    case invalidOrigin, invalidMedia, invalidPairLink, invalidCommand, keychain
    var errorDescription: String? {
        switch self {
        case .invalidOrigin: return "Нужен HTTPS-адрес вашего XASS без логина и пароля в адресе."
        case .invalidMedia: return "Небезопасная или устаревшая ссылка на музыку. Откройте трек заново."
        case .invalidPairLink: return "Нужна одноразовая ссылка XASS из Telegram Mini App этого сервера."
        case .invalidCommand: return "Команда приложения не поддерживается."
        case .keychain: return "Не удалось сохранить защищённые данные на устройстве."
        }
    }
}

struct ServerOrigin: Equatable {
    let url: URL
    let host: String
    let port: Int
    var namespace: String { SHA256.hash(data: Data(url.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined() }
    var entryURL: URL { URL(string: "miniapp.php?standalone=1", relativeTo: url.appendingPathComponent("/"))!.absoluteURL }

    init(_ raw: String) throws {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= 8192,
              !text.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              !text.contains("\\"), let parts = URLComponents(string: text),
              parts.scheme?.lowercased() == "https", parts.user == nil, parts.password == nil,
              let domain = parts.host?.lowercased(), !domain.isEmpty, !domain.contains("%"),
              (parts.port ?? 443) > 0, (parts.port ?? 443) <= 65535,
              var clean = URLComponents(string: "https://" + domain) else { throw XASSErr.invalidOrigin }
        // URLComponents rebuild also handles IPv6 literals safely.
        clean.host = domain
        clean.port = parts.port == 443 ? nil : parts.port
        clean.path = ""
        guard let value = clean.url, value.host != nil else { throw XASSErr.invalidOrigin }
        url = value; host = domain; port = parts.port ?? 443
    }

    func contains(_ candidate: URL) -> Bool {
        guard let parts = URLComponents(url: candidate, resolvingAgainstBaseURL: true),
              parts.scheme?.lowercased() == "https", parts.user == nil, parts.password == nil,
              parts.host?.lowercased() == host, (parts.port ?? 443) == port,
              !candidate.absoluteString.contains("\\") else { return false }
        return true
    }

    func loginURL(pairInput: String) throws -> URL {
        let raw = pairInput.trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.isEmpty { return entryURL }
        var token = raw
        if !raw.hasPrefix("xpw_") {
            guard let link = URL(string: raw), contains(link), link.path == "/miniapp.php",
                  let fragment = URLComponents(url: link, resolvingAgainstBaseURL: false)?.fragment,
                  let parsed = URLComponents(string: "https://fragment.invalid/?" + fragment),
                  parsed.queryItems?.count == 1, parsed.queryItems?.first?.name == "pair",
                  let value = parsed.queryItems?.first?.value else { throw XASSErr.invalidPairLink }
            token = value
        }
        guard token.range(of: #"^xpw_[A-Za-z0-9_-]{28,200}$"#, options: .regularExpression) != nil else { throw XASSErr.invalidPairLink }
        var entry = URLComponents(url: entryURL, resolvingAgainstBaseURL: false)!
        entry.fragment = "pair=" + token
        return entry.url!
    }

    func mediaURL(_ raw: String, trackID: Int) throws -> URL {
        guard raw.count <= 16384, trackID > 0,
              let value = URL(string: raw, relativeTo: url.appendingPathComponent("/"))?.absoluteURL,
              contains(value), let outer = URLComponents(url: value, resolvingAgainstBaseURL: false),
              outer.fragment == nil else { throw XASSErr.invalidMedia }
        let path: String
        if outer.path == "/proxy.php" {
            let items = outer.queryItems ?? []
            guard items.count == 3, items.filter({ $0.name == "_binary" && $0.value == "1" }).count == 1,
                  items.filter({ $0.name == "_media" && $0.value == "1" }).count == 1,
                  let nested = items.first(where: { $0.name == "_p" })?.value else { throw XASSErr.invalidMedia }
            path = nested
        } else { path = outer.path + (outer.percentEncodedQuery.map { "?" + $0 } ?? "") }
        guard let inner = URLComponents(string: path), inner.scheme == nil, inner.host == nil,
              inner.path == "/api/music/tracks/\(trackID)/stream", inner.fragment == nil,
              let query = inner.queryItems, query.count == 1, query[0].name == "ticket",
              let ticket = query[0].value, !ticket.isEmpty, ticket.count <= 4096 else { throw XASSErr.invalidMedia }
        return value
    }
}

enum SecureStore {
    private static let service = "app.xass.mobile"
    static func load(_ name: String) -> Data? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: name,
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess else { return nil }
        return result as? Data
    }
    static func save(_ data: Data, name: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: name]
        let attrs: [String: Any] = [kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        let result = SecItemUpdate(query as CFDictionary, attrs as CFDictionary)
        if result == errSecItemNotFound {
            guard SecItemAdd(query.merging(attrs) { _, new in new } as CFDictionary, nil) == errSecSuccess else { throw XASSErr.keychain }
        } else if result != errSecSuccess { throw XASSErr.keychain }
    }
    static func remove(_ name: String) {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: name] as CFDictionary)
    }
}
