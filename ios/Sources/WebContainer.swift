import SwiftUI
import WebKit

@MainActor final class WebController: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKHTTPCookieStoreObserver {
    @Published var error: String?
    @Published var loading = true
    @Published var popup: WKWebView?
    let webView: WKWebView
    let origin: ServerOrigin
    private weak var app: AppState?
    private let reporter: SessionReporter
    private var cookieReady = false
    private var closed = false

    init(app: AppState, origin: ServerOrigin) {
        self.app = app; self.origin = origin; reporter = SessionReporter(origin: origin)
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        webView.navigationDelegate = self; webView.uiDelegate = self
        webView.isOpaque = false; webView.backgroundColor = .black; webView.scrollView.backgroundColor = .black
        webView.allowsBackForwardNavigationGestures = true
        // The bridge is only advertised on the configured origin, never Telegram frames.
        let quoted = String(data: try! JSONSerialization.data(withJSONObject: [origin.url.absoluteString]), encoding: .utf8)!
        let script = "if(window.top===window && location.origin===" + quoted + "[0]){window.XASS_NATIVE_AUDIO=true;}"
        configuration.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        configuration.userContentController.add(self, name: "xassAudio")
        app.audio.emit = { [weak self] detail in self?.sendEvent(detail) }
        app.audio.reportSession = { [weak self] body in self?.reporter.post(body) }
        app.audio.requestTicket = { [weak self] id, completion in
            guard let self = self else { completion(.failure(XASSErr.invalidMedia)); return }
            self.reporter.ticket(trackID: id, completion: completion)
        }
        restoreSession()
    }
    func close() {
        closed = true; cookieReady = false
        webView.stopLoading(); popup?.stopLoading(); popup = nil
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "xassAudio")
        webView.configuration.websiteDataStore.httpCookieStore.remove(self)
        reporter.invalidate()
    }
    private func restoreSession() {
        let store = webView.configuration.websiteDataStore.httpCookieStore
        store.getAllCookies { [weak self] cookies in
            guard let self = self, !self.closed, self.app?.origin == self.origin else { return }
            let ready = {
                guard !self.closed, self.app?.origin == self.origin else { return }
                self.cookieReady = true; store.add(self); self.cookiesDidChange(in: store)
                self.webView.load(URLRequest(url: self.app?.entryURL ?? self.origin.entryURL))
            }
            if cookies.contains(where: { self.isSession($0) && ($0.expiresDate ?? .distantFuture) > Date() }) { ready(); return }
            guard let data = SecureStore.load("session-" + self.origin.namespace),
                  let saved = try? JSONDecoder().decode(SavedSession.self, from: data), saved.expires > Date(),
                  let cookie = HTTPCookie(properties: [.name: "xass_pwa", .value: saved.value,
                    .domain: self.origin.host, .path: "/", .secure: "TRUE", .expires: saved.expires,
                    HTTPCookiePropertyKey("HttpOnly"): "TRUE"]) else { ready(); return }
            store.setCookie(cookie, completionHandler: ready)
        }
    }
    private func isSession(_ cookie: HTTPCookie) -> Bool {
        cookie.name == "xass_pwa" && cookie.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased() == origin.host && cookie.path == "/" && cookie.isSecure && cookie.isHTTPOnly
    }
    func cookiesDidChange(in cookieStore: WKHTTPCookieStore) {
        guard cookieReady else { return }
        cookieStore.getAllCookies { [weak self] cookies in
            guard let self = self, !self.closed, self.app?.origin == self.origin else { return }
            if let cookie = cookies.first(where: { self.isSession($0) && ($0.expiresDate ?? .distantFuture) > Date() }) {
                let saved = SavedSession(value: cookie.value, expires: cookie.expiresDate ?? Date().addingTimeInterval(30 * 86400))
                if let data = try? JSONEncoder().encode(saved) { try? SecureStore.save(data, name: "session-" + self.origin.namespace) }
            } else { SecureStore.remove("session-" + self.origin.namespace) }
        }
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        let frame = message.frameInfo
        guard let app = app, !app.locked, !app.privacyCovered, app.origin == origin,
              message.name == "xassAudio", frame.isMainFrame,
              frame.securityOrigin.protocol == "https", frame.securityOrigin.host.lowercased() == origin.host,
              (frame.securityOrigin.port == 0 ? 443 : frame.securityOrigin.port) == origin.port,
              message.webView === webView, let current = webView.url, origin.contains(current),
              let body = message.body as? [String: Any] else { return }
        do { app.audio.handle(try NativeAudioCommand(body)) }
        catch { sendEvent(["state": "error", "trackId": 0, "position": 0, "duration": 0, "error": "Небезопасная команда отклонена."]) }
    }
    private func sendEvent(_ detail: [String: Any]) {
        guard UIApplication.shared.applicationState == .active, let current = webView.url, origin.contains(current),
              let data = try? JSONSerialization.data(withJSONObject: detail), let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('xass:native-audio',{detail:" + json + "}));", completionHandler: nil)
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if navigationAction.targetFrame?.isMainFrame == false { decisionHandler(url.scheme == "https" || url.absoluteString == "about:blank" ? .allow : .cancel); return }
        if origin.contains(url) { decisionHandler(.allow); return }
        if webView === popup && Self.telegramLogin(url) { decisionHandler(.allow); return }
        decisionHandler(.cancel)
        if navigationAction.navigationType == .linkActivated && ["https", "tg"].contains(url.scheme?.lowercased() ?? "") {
            UIApplication.shared.open(url)
        }
    }
    static func telegramLogin(_ url: URL) -> Bool {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false), parts.scheme == "https", parts.user == nil, parts.password == nil,
              (parts.port ?? 443) == 443 else { return false }
        return ["oauth.telegram.org", "telegram.org"].contains(parts.host?.lowercased() ?? "")
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil, let url = navigationAction.request.url,
              Self.telegramLogin(url) || origin.contains(url) else { return nil }
        let child = WKWebView(frame: .zero, configuration: configuration)
        child.navigationDelegate = self; child.uiDelegate = self; popup = child
        return child
    }
    func webViewDidClose(_ webView: WKWebView) { if webView === popup { popup = nil } }
    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) { if webView === self.webView { loading = true; error = nil } }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === self.webView { loading = false; app?.entryURL = origin.entryURL }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { failed(webView, error) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(webView, error) }
    private func failed(_ view: WKWebView, _ error: Error) {
        if view === webView && (error as NSError).code != NSURLErrorCancelled {
            loading = false; self.error = "Не удалось открыть сервер. Проверьте HTTPS-адрес и подключение. Сохранённая музыка доступна в «Загрузках»."
        }
    }
    func retry() { webView.load(URLRequest(url: app?.entryURL ?? origin.entryURL)) }
}

struct WebSurface: UIViewRepresentable {
    let webView: WKWebView
    func makeUIView(context: Context) -> WKWebView { webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

@MainActor struct WebContainer: View {
    @StateObject private var controller: WebController
    @ObservedObject private var app: AppState
    init(app: AppState, origin: ServerOrigin) { self.app = app; _controller = StateObject(wrappedValue: WebController(app: app, origin: origin)) }
    var body: some View {
        ZStack {
            WebSurface(webView: controller.webView)
            if controller.loading { ProgressView().padding(12).background(.ultraThinMaterial, in: Capsule()).frame(maxHeight: .infinity, alignment: .top).padding(.top, 8) }
            if let error = controller.error {
                VStack(spacing: 18) { Image(systemName: "wifi.slash").font(.largeTitle); Text(error).multilineTextAlignment(.center); Button("Повторить") { controller.retry() }.buttonStyle(.borderedProminent) }
                    .padding(28).frame(maxWidth: 420).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24)).padding()
            }
        }
        .sheet(isPresented: Binding(get: { controller.popup != nil }, set: { if !$0 { controller.popup?.stopLoading(); controller.popup = nil } })) {
            NavigationStack { if let popup = controller.popup { WebSurface(webView: popup).navigationTitle("Вход через Telegram").navigationBarTitleDisplayMode(.inline).toolbar { Button("Готово") { controller.popup = nil } } } }
        }
        .onChange(of: app.locked) { _, locked in if locked { controller.popup?.stopLoading(); controller.popup = nil } }
        .onDisappear { controller.close() }
    }
}
