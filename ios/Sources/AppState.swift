import SwiftUI
import LocalAuthentication
import WebKit

@MainActor final class AppState: ObservableObject {
    @Published var origin: ServerOrigin?
    @Published var entryURL: URL?
    @Published private(set) var locked = true
    @Published private(set) var privacyCovered = true
    @Published private(set) var authenticating = false
    @Published private(set) var hasUnlocked = false
    @Published var error: String?
    @Published var showSettings = false
    @Published var native: NativeStore?
    @Published var nativeConfirmation = false
    @Published private(set) var clearingSession = false
    let audio = AudioController()
    private var authContext: LAContext?
    private var authGeneration = UUID()
    private var foreground = false
    private var pendingUnlock = false
    private var pendingNativePair: String?

    init() {
        #if DEBUG && targetEnvironment(simulator)
        if NativeFixture.enabled, let server = try? ServerOrigin("https://native-fixture.invalid") {
            origin = server; entryURL = server.entryURL
            native = NativeStore(api: NativeFixture(origin: server), audio: audio)
            native?.installFixture(); locked = false; privacyCovered = false; hasUnlocked = true
            return
        }
        #endif
        if let data = SecureStore.load("origin"), let text = String(data: data, encoding: .utf8), let server = try? ServerOrigin(text) {
            origin = server; entryURL = server.entryURL; audio.configure(server)
            configureNative(server)
        }
    }
    func connect(address: String, pair: String) {
        guard !clearingSession else { error = "Завершаю выход из предыдущего сервера…"; return }
        do {
            let server = try ServerOrigin(address)
            // A pasted complete pair link can be used directly as the address.
            let input = pair.isEmpty && address.contains("#pair=") ? address : pair
            _ = try server.loginURL(pairInput: input)
            try SecureStore.save(Data(server.url.absoluteString.utf8), name: "origin")
            origin = server; entryURL = server.entryURL; audio.configure(server); configureNative(server)
            pendingNativePair = input.isEmpty ? nil : input
            error = nil; locked = true; authenticate()
        } catch { self.error = error.localizedDescription }
    }
    func scene(_ phase: ScenePhase) {
        #if DEBUG && targetEnvironment(simulator)
        if NativeFixture.enabled { return }
        #endif
        foreground = phase == .active
        if phase == .background {
            native?.foreground(false)
            privacyCovered = true; locked = true; pendingUnlock = false
            authGeneration = UUID(); authContext?.invalidate(); authenticating = false
        } else if phase == .inactive {
            privacyCovered = true
            if !authenticating && !nativeConfirmation { locked = true }
        } else {
            privacyCovered = false
            if pendingUnlock { pendingUnlock = false; locked = false; hasUnlocked = true; activateNative() }
            else if origin != nil && locked && !authenticating { authenticate() }
            else if !locked { native?.foreground(true) }
        }
    }
    func cover() {
        #if DEBUG && targetEnvironment(simulator)
        if NativeFixture.enabled { return }
        #endif
        privacyCovered = true; if !authenticating && !nativeConfirmation { locked = true }
    }
    private func configureNative(_ origin: ServerOrigin) {
        native?.disconnect()
        let store = NativeStore(api: OwnerAPI(origin: origin), audio: audio)
        store.confirmationActivity = { [weak self] active in self?.nativeConfirmation = active }
        store.canSendActions = { [weak self] in guard let self = self else { return false }; return !self.locked && !self.privacyCovered && UIApplication.shared.applicationState == .active }
        native = store
    }
    private func activateNative() {
        guard let store = native else { return }
        if let pair = pendingNativePair {
            pendingNativePair = nil
            store.run { try await store.enroll(pairInput: pair); store.foreground(true) }
        } else { store.foreground(true) }
    }
    func authenticate() {
        guard origin != nil, !authenticating else { return }
        let context = LAContext(); context.localizedCancelTitle = "Отмена"
        var failure: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &failure) else {
            error = "Включите код-пароль iPhone в настройках устройства. Он нужен для защиты XASS."; return
        }
        authenticating = true; error = nil; authContext = context
        let generation = UUID(); authGeneration = generation
        context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "Открыть управление XASS") { [weak self] success, _ in
            Task { @MainActor in
                guard let self = self, self.authGeneration == generation else { return }
                self.authenticating = false; self.authContext = nil
                if success {
                    if self.foreground { self.locked = false; self.hasUnlocked = true; self.activateNative() }
                    else { self.pendingUnlock = true }
                } else { self.locked = true; self.error = "Подтверждение отменено. Нажмите «Открыть XASS», чтобы повторить." }
            }
        }
    }
    func forgetServer() {
        // Explicit settings confirmation revokes this app's local login only.
        // Server data and the user's downloaded audio are not deleted.
        guard !clearingSession else { return }
        clearingSession = true
        if let origin = origin { SecureStore.remove("session-" + origin.namespace) }
        SecureStore.remove("origin"); native?.disconnect(); native = nil; audio.disconnect(); origin = nil; entryURL = nil; pendingNativePair = nil
        hasUnlocked = false; locked = true; showSettings = false; error = nil
        WKWebsiteDataStore.default().removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast) { [weak self] in
            Task { @MainActor in self?.clearingSession = false }
        }
    }
}

struct SavedSession: Codable {
    let value: String
    let expires: Date
}

enum ServerEnvelope {
    static func decode(_ data: Data, httpStatus: Int) throws -> [String: Any] {
        guard (200..<300).contains(httpStatus), data.count <= 32 * 1024,
              let outer = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw XASSErr.invalidMedia }
        // PHP proxy.php uses _s and JSON-in-string _b, unlike the direct API.
        if outer["_s"] != nil || outer["_b"] != nil {
            guard let status = outer["_s"] as? Int, (200..<300).contains(status),
                  let text = outer["_b"] as? String, let inner = text.data(using: .utf8), inner.count <= 32 * 1024,
                  let body = try JSONSerialization.jsonObject(with: inner) as? [String: Any] else { throw XASSErr.invalidMedia }
            return body
        }
        return outer
    }
}

/// Session-cookie persistence never passes the HttpOnly cookie into JavaScript.
@MainActor final class SessionReporter: NSObject, URLSessionTaskDelegate {
    let origin: ServerOrigin
    private var pending: [String: Any]?
    private var busy = false
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil; config.httpShouldSetCookies = false; config.urlCache = nil
        config.timeoutIntervalForRequest = 8
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()
    init(origin: ServerOrigin) { self.origin = origin; super.init() }
    func post(_ body: [String: Any]) {
        pending = body
        flush()
    }
    private func flush() {
        guard !busy, let body = pending, let request = makeRequest(path: "/api/mini/music/session", body: body) else { return }
        pending = nil; busy = true
        session.dataTask(with: request) { [weak self] _, _, _ in
            Task { @MainActor in self?.busy = false; self?.flush() }
        }.resume()
    }
    private func makeRequest(path: String, body: [String: Any]) -> URLRequest? {
        guard let data = SecureStore.load("session-" + origin.namespace),
              let saved = try? JSONDecoder().decode(SavedSession.self, from: data), saved.expires > Date(),
              !saved.value.contains("\r"), !saved.value.contains("\n"), saved.value.count < 8192 else { return nil }
        var parts = URLComponents(url: origin.url.appendingPathComponent("proxy.php"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "_p", value: path)]
        var request = URLRequest(url: parts.url!)
        request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("xass_pwa=" + saved.value, forHTTPHeaderField: "Cookie")
        request.setValue(origin.url.absoluteString, forHTTPHeaderField: "Origin")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        return request
    }
    func ticket(trackID: Int, completion: @escaping (Result<String, Error>) -> Void) {
        guard trackID > 0, let request = makeRequest(path: "/api/mini/music/tracks/\(trackID)/ticket", body: ["purpose": "listen"]) else { completion(.failure(XASSErr.invalidMedia)); return }
        session.dataTask(with: request) { [weak self] data, response, error in
            Task { @MainActor in
                guard let self = self, error == nil, let response = response as? HTTPURLResponse, response.statusCode == 200,
                      let data = data,
                      let body = try? ServerEnvelope.decode(data, httpStatus: response.statusCode) else { completion(.failure(XASSErr.invalidMedia)); return }
                guard body["ok"] as? Bool == true, let path = body["path"] as? String,
                      (try? self.origin.mediaURL(path, trackID: trackID)) != nil else { completion(.failure(XASSErr.invalidMedia)); return }
                var parts = URLComponents(url: self.origin.url.appendingPathComponent("proxy.php"), resolvingAgainstBaseURL: false)!
                parts.queryItems = [.init(name: "_binary", value: "1"), .init(name: "_media", value: "1"), .init(name: "_p", value: path)]
                completion(.success(parts.url!.absoluteString))
            }
        }.resume()
    }
    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // A session mutation must never redirect, even within the origin.
        completionHandler(nil)
    }
    func invalidate() { pending = nil; session.invalidateAndCancel() }
}
