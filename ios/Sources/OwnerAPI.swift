import Foundation

struct OwnerAPIError: LocalizedError {
    let status: Int
    let message: String
    let detail: [String: Any]?
    init(status: Int, message: String, detail: [String: Any]? = nil) { self.status = status; self.message = message; self.detail = detail }
    var errorDescription: String? { message }
    static let signedOut = OwnerAPIError(status: 401, message: "Войдите на свой сервер заново. Сохранённая музыка остаётся на iPhone.")
    static let invalidResponse = OwnerAPIError(status: 0, message: "Сервер вернул неподдерживаемый ответ. Проверьте адрес и версию XASS.")
}

protocol OwnerService: AnyObject {
    var origin: ServerOrigin { get }
    @MainActor func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any]
    @MainActor func artwork(trackID: Int) async throws -> Data?
    @MainActor func binary(_ path: String) async throws -> Data
}
extension OwnerService {
    @MainActor func artwork(trackID: Int) async throws -> Data? { nil }
    @MainActor func binary(_ path: String) async throws -> Data { throw OwnerAPIError.invalidResponse }
}

/// Native API transport. An HttpOnly owner cookie never passes through JavaScript;
/// requests, redirects and response sizes are bounded independently of the UI.
final class OwnerAPI: NSObject, OwnerService, URLSessionDataDelegate, @unchecked Sendable {
    let origin: ServerOrigin
    static let maxResponseBytes = 4 * 1024 * 1024
    static let maxAssetBytes = 16 * 1024 * 1024
    private let configuration: URLSessionConfiguration
    private let savedSession: () -> SavedSession?
    private let saveSession: (SavedSession) -> Void
    private enum Completion {
        case json(CheckedContinuation<[String: Any], Error>)
        case artwork(CheckedContinuation<Data?, Error>)
        case binary(CheckedContinuation<Data, Error>)
        var limit: Int { switch self { case .json: return OwnerAPI.maxResponseBytes; case .artwork: return 1024 * 1024; case .binary: return OwnerAPI.maxAssetBytes } }
        func fail(_ error: Error) { switch self { case .json(let done): done.resume(throwing: error); case .artwork(let done): done.resume(throwing: error); case .binary(let done): done.resume(throwing: error) } }
    }
    private struct Transfer {
        var data = Data()
        var response: HTTPURLResponse?
        let completion: Completion
    }
    // URLSession delegates run on .main, like the @MainActor request entrypoint.
    private var transfers: [Int: Transfer] = [:]
    private lazy var session: URLSession = URLSession(configuration: configuration, delegate: self, delegateQueue: .main)

    init(origin: ServerOrigin, configuration: URLSessionConfiguration = .ephemeral,
         savedSession: (() -> SavedSession?)? = nil, saveSession: ((SavedSession) -> Void)? = nil) {
        self.origin = origin; self.configuration = configuration
        self.savedSession = savedSession ?? {
            guard let data = SecureStore.load("session-" + origin.namespace) else { return nil }
            return try? JSONDecoder().decode(SavedSession.self, from: data)
        }
        self.saveSession = saveSession ?? { saved in
            if let data = try? JSONEncoder().encode(saved) { try? SecureStore.save(data, name: "session-" + origin.namespace) }
        }
        configuration.httpCookieStorage = nil; configuration.httpShouldSetCookies = false
        configuration.urlCache = nil; configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 12; configuration.timeoutIntervalForResource = 35
        super.init()
    }

    @MainActor func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> [String: Any] {
        let nativePaths = ["/api/native/enrollment/options", "/api/native/enrollment/verify", "/api/native/actions/options", "/api/native/actions/verify"]
        let enrollment = path == "/api/native/enrollment/options" || path == "/api/native/enrollment/verify"
        guard (path.hasPrefix("/api/mini/") || nativePaths.contains(path)), !path.contains(".."), !path.contains("\\"), !path.contains("#"),
              !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              ["GET", "POST", "PUT", "PATCH", "DELETE"].contains(method), !nativePaths.contains(path) || method == "POST" else { throw OwnerAPIError.invalidResponse }
        let saved = savedSession()
        let validSession = saved.map { $0.expires > Date() && !$0.value.isEmpty && $0.value.utf8.count < 8192 && !$0.value.contains("\r") && !$0.value.contains("\n") && !$0.value.contains(";") } ?? false
        guard validSession || enrollment else { throw OwnerAPIError.signedOut }
        var parts = URLComponents(url: origin.url.appendingPathComponent("proxy.php"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [.init(name: "_p", value: path)]
        var request = URLRequest(url: parts.url!)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        request.setValue(origin.url.absoluteString, forHTTPHeaderField: "Origin")
        if validSession, let saved = saved { request.setValue("xass_pwa=" + saved.value, forHTTPHeaderField: "Cookie") }
        if let body = body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        try Task.checkCancellation()
        let value: [String: Any] = try await withCheckedThrowingContinuation { completion in
            let task = session.dataTask(with: request)
            transfers[task.taskIdentifier] = Transfer(completion: .json(completion))
            task.resume()
        }
        // Mutations must return their receipt even when the calling view was
        // cancelled, so callers can undo a handoff by its server-issued ID.
        if method == "GET" { try Task.checkCancellation() }
        return value
    }
    @MainActor func artwork(trackID: Int) async throws -> Data? {
        try Task.checkCancellation()
        guard trackID > 0, let saved = savedSession(), saved.expires > Date(), !saved.value.isEmpty,
              saved.value.count < 8192, !saved.value.contains("\r"), !saved.value.contains("\n"), !saved.value.contains(";") else { throw OwnerAPIError.signedOut }
        var parts = URLComponents(url: origin.url.appendingPathComponent("proxy.php"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [.init(name: "_binary", value: "1"), .init(name: "_p", value: "/api/mini/music/tracks/\(trackID)/artwork")]
        var request = URLRequest(url: parts.url!)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("image/jpeg", forHTTPHeaderField: "Accept")
        request.setValue("xass_pwa=" + saved.value, forHTTPHeaderField: "Cookie")
        request.setValue(origin.url.absoluteString, forHTTPHeaderField: "Origin")
        let value: Data? = try await withCheckedThrowingContinuation { done in
            let task = session.dataTask(with: request); transfers[task.taskIdentifier] = Transfer(completion: .artwork(done)); task.resume()
        }
        try Task.checkCancellation()
        return value
    }

    static func allowsBinaryPath(_ path: String) -> Bool {
        guard !path.contains("\\"), !path.contains(".."),
              !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              let decoded = path.removingPercentEncoding, !decoded.contains(".."), !decoded.contains("\\") else { return false }
        return path.range(of: #"^/api/mini/(media/[1-9][0-9]*|agents/[^/?#]+/assets/[A-Za-z0-9_-]+)$"#, options: .regularExpression) != nil
    }

    @MainActor func binary(_ path: String) async throws -> Data {
        guard Self.allowsBinaryPath(path) else { throw OwnerAPIError.invalidResponse }
        guard let saved = savedSession(), saved.expires > Date(), !saved.value.isEmpty,
              saved.value.utf8.count < 8192, !saved.value.contains("\r"), !saved.value.contains("\n"), !saved.value.contains(";") else { throw OwnerAPIError.signedOut }
        var parts = URLComponents(url: origin.url.appendingPathComponent("proxy.php"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [.init(name: "_binary", value: "1"), .init(name: "_p", value: path)]
        var request = URLRequest(url: parts.url!)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/octet-stream", forHTTPHeaderField: "Accept")
        request.setValue("xass_pwa=" + saved.value, forHTTPHeaderField: "Cookie")
        request.setValue(origin.url.absoluteString, forHTTPHeaderField: "Origin")
        try Task.checkCancellation()
        let value: Data = try await withCheckedThrowingContinuation { done in
            let task = session.dataTask(with: request)
            transfers[task.taskIdentifier] = Transfer(completion: .binary(done)); task.resume()
        }
        try Task.checkCancellation()
        return value
    }

    static func decode(_ data: Data, status: Int) throws -> [String: Any] {
        guard data.count <= maxResponseBytes else { throw OwnerAPIError.invalidResponse }
        var code = status
        var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        if let envelope = object, envelope["_s"] != nil || envelope["_b"] != nil {
            guard let innerStatus = envelope["_s"] as? Int, let text = envelope["_b"] as? String,
                  let inner = text.data(using: .utf8), inner.count <= maxResponseBytes else { throw OwnerAPIError.invalidResponse }
            if (200..<300).contains(status) { code = innerStatus }
            object = (try? JSONSerialization.jsonObject(with: inner)) as? [String: Any]
        }
        guard (200..<300).contains(code) else {
            if code == 401 { throw OwnerAPIError.signedOut }
            let detailObject = object?["detail"] as? [String: Any]
            let detail = ((object?["detail"] as? String) ?? (detailObject?["message"] as? String)).map { String($0.prefix(500)) }
            throw OwnerAPIError(status: code, message: detail ?? (code >= 500 ? "Сервер временно недоступен. Повторите позже." : "Сервер не разрешил действие. Обновите данные и повторите."), detail: object?["detail"] as? [String: Any])
        }
        guard let object = object, object["ok"] as? Bool == true else { throw OwnerAPIError.invalidResponse }
        return object
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil) // Do not forward owner credentials, including to same-origin redirects.
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse, let url = http.url, origin.contains(url),
              let transfer = transfers[dataTask.taskIdentifier],
              response.expectedContentLength <= Int64(transfer.completion.limit) else {
            completionHandler(.cancel); finish(dataTask.taskIdentifier, error: OwnerAPIError.invalidResponse); return
        }
        transfers[dataTask.taskIdentifier]?.response = http; completionHandler(.allow)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard let current = transfers[dataTask.taskIdentifier], current.data.count + data.count <= current.completion.limit else {
            dataTask.cancel(); finish(dataTask.taskIdentifier, error: OwnerAPIError.invalidResponse); return
        }
        transfers[dataTask.taskIdentifier]?.data.append(data)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) { finish(task.taskIdentifier, error: error) }
    private func finish(_ id: Int, error: Error?) {
        guard let transfer = transfers.removeValue(forKey: id) else { return }
        if let error = error { transfer.completion.fail(error); return }
        guard let status = transfer.response?.statusCode else { transfer.completion.fail(OwnerAPIError.invalidResponse); return }
        if case .binary(let done) = transfer.completion {
            guard status == 200, !transfer.data.isEmpty else {
                done.resume(throwing: status == 401 ? OwnerAPIError.signedOut : OwnerAPIError(status: status, message: "Файл недоступен. Запросите его заново.")); return
            }
            let mime = transfer.response?.mimeType?.lowercased() ?? ""
            guard mime != "application/json", mime != "text/html" else { done.resume(throwing: OwnerAPIError.invalidResponse); return }
            done.resume(returning: transfer.data); return
        }
        if case .artwork(let done) = transfer.completion {
            if status == 404 { done.resume(returning: nil); return }
            guard status == 200, transfer.response?.mimeType == "image/jpeg", transfer.data.count <= 1024 * 1024 else { done.resume(throwing: OwnerAPIError.invalidResponse); return }
            done.resume(returning: transfer.data); return
        }
        do {
            let body = try Self.decode(transfer.data, status: status)
            if let response = transfer.response, let url = response.url {
                let headers = response.allHeaderFields.reduce(into: [String: String]()) { values, item in values[String(describing: item.key)] = String(describing: item.value) }
                if let cookie = HTTPCookie.cookies(withResponseHeaderFields: headers, for: url).first(where: {
                    $0.name == "xass_pwa" && $0.isSecure && $0.isHTTPOnly && $0.path == "/" && $0.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased() == origin.host
                }), let expires = cookie.expiresDate, expires > Date() { saveSession(SavedSession(value: cookie.value, expires: expires)) }
            }
            if case .json(let done) = transfer.completion { done.resume(returning: body) }
        }
        catch { transfer.completion.fail(error) }
    }
    @MainActor func invalidate() {
        session.invalidateAndCancel()
        for id in Array(transfers.keys) { finish(id, error: URLError(.cancelled)) }
    }
    static func pathComponent(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_~"))) ?? ""
    }
}
