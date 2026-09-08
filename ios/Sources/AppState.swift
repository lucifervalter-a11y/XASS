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
    let audio = AudioController()
    private var authContext: LAContext?
    private var authGeneration = UUID()
    private var foreground = false
    private var pendingUnlock = false

    init() {
        if let data = SecureStore.load("origin"), let text = String(data: data, encoding: .utf8), let server = try? ServerOrigin(text) {
            origin = server; entryURL = server.entryURL; audio.configure(server)
        }
    }
    func connect(address: String, pair: String) {
        do {
            let server = try ServerOrigin(address)
            // A pasted complete pair link can be used directly as the address.
            let input = pair.isEmpty && address.contains("#pair=") ? address : pair
            let entry = try server.loginURL(pairInput: input)
            try SecureStore.save(Data(server.url.absoluteString.utf8), name: "origin")
            origin = server; entryURL = entry; audio.configure(server)
            error = nil; locked = true; authenticate()
        } catch { self.error = error.localizedDescription }
    }
    func scene(_ phase: ScenePhase) {
        foreground = phase == .active
        if phase == .background {
            privacyCovered = true; locked = true; pendingUnlock = false
            authGeneration = UUID(); authContext?.invalidate(); authenticating = false
        } else if phase == .inactive {
            privacyCovered = true
            if !authenticating { locked = true }
        } else {
            privacyCovered = false
            if pendingUnlock { pendingUnlock = false; locked = false; hasUnlocked = true }
            else if origin != nil && locked && !authenticating { authenticate() }
        }
    }
    func cover() { privacyCovered = true; if !authenticating { locked = true } }
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
                    if self.foreground { self.locked = false; self.hasUnlocked = true }
                    else { self.pendingUnlock = true }
                } else { self.locked = true; self.error = "Подтверждение отменено. Нажмите «Открыть XASS», чтобы повторить." }
            }
        }
    }
    func forgetServer() {
        // Explicit settings confirmation revokes this app's local login only.
        // Server data and the user's downloaded audio are not deleted.
        if let origin = origin { SecureStore.remove("session-" + origin.namespace) }
        SecureStore.remove("origin"); audio.disconnect(); origin = nil; entryURL = nil
        hasUnlocked = false; locked = true; showSettings = false; error = nil
        WKWebsiteDataStore.default().removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast) {}
    }
}

struct SavedSession: Codable {
    let value: String
    let expires: Date
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
        guard !busy, let body = pending,
              let data = SecureStore.load("session-" + origin.namespace),
              let saved = try? JSONDecoder().decode(SavedSession.self, from: data), saved.expires > Date(),
              !saved.value.contains("\r"), !saved.value.contains("\n"), saved.value.count < 8192 else { return }
        var parts = URLComponents(url: origin.url.appendingPathComponent("proxy.php"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "_p", value: "/api/mini/music/session")]
        var request = URLRequest(url: parts.url!)
        request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("xass_pwa=" + saved.value, forHTTPHeaderField: "Cookie")
        request.setValue(origin.url.absoluteString, forHTTPHeaderField: "Origin")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        pending = nil; busy = true
        session.dataTask(with: request) { [weak self] _, _, _ in
            Task { @MainActor in self?.busy = false; self?.flush() }
        }.resume()
    }
    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // A session mutation must never redirect, even within the origin.
        completionHandler(nil)
    }
    func invalidate() { pending = nil; session.invalidateAndCancel() }
}
