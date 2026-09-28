import Darwin
import Foundation
import Network
import Security

/// Serves one already-downloaded track to the paired computer on the same LAN.
/// The listener is token-gated, GET-only, and closes after the file is sent or three minutes.
final class NativeLanOffer: @unchecked Sendable {
    struct Offer: Equatable {
        let host: String
        let port: Int
        let token: String
    }

    private let queue = DispatchQueue(label: "xass.lan.offer")
    private let lock = NSLock()
    private var listener: NWListener?
    private var fileURL: URL?
    private var trackID = 0
    private var token = ""
    private var stopped = false
    private var busy = false
    private var deadline = Date.distantPast

    func start(file: URL, trackID: Int) async -> Offer? {
        guard trackID > 0, let host = Self.privateIPv4(), let token = Self.makeToken() else { return nil }
        return await withCheckedContinuation { continuation in
            let box = ResumeOnce(continuation)
            let parameters = NWParameters.tcp
            parameters.allowLocalEndpointReuse = true
            guard let listener = try? NWListener(using: parameters, on: .any) else {
                box.resume(nil)
                return
            }
            listener.stateUpdateHandler = { [weak self] state in
                guard let self else { box.resume(nil); return }
                switch state {
                case .ready:
                    let port = Int(listener.port?.rawValue ?? 0)
                    guard port >= 1024 else {
                        listener.cancel()
                        box.resume(nil)
                        return
                    }
                    self.arm(listener, file: file, trackID: trackID, token: token)
                    if !box.resume(Offer(host: host, port: port, token: token)) {
                        listener.cancel()
                    }
                case .failed, .cancelled:
                    box.resume(nil)
                default:
                    break
                }
            }
            listener.newConnectionHandler = { [weak self] connection in
                self?.accept(connection)
            }
            listener.start(queue: queue)
            queue.asyncAfter(deadline: .now() + 2) {
                if box.resume(nil) {
                    listener.cancel()
                }
            }
        }
    }

    func stop() {
        queue.async { [weak self] in
            self?.shutdown()
        }
    }

    private func arm(_ listener: NWListener, file: URL, trackID: Int, token: String) {
        lock.lock()
        self.listener = listener
        self.fileURL = file
        self.trackID = trackID
        self.token = token
        self.stopped = false
        self.deadline = Date().addingTimeInterval(180)
        lock.unlock()
        queue.asyncAfter(deadline: .now() + 180) { [weak self] in
            self?.shutdown()
        }
    }

    private func shutdown() {
        lock.lock()
        stopped = true
        let current = listener
        listener = nil
        lock.unlock()
        current?.cancel()
    }

    private func accept(_ connection: NWConnection) {
        lock.lock()
        let expired = stopped || Date() >= deadline
        let occupied = busy
        if !expired && !occupied { busy = true }
        let expectedToken = token
        let expectedTrack = trackID
        let file = fileURL
        lock.unlock()
        connection.start(queue: queue)
        if expired || file == nil {
            reply(connection, status: 404, release: !expired)
            return
        }
        if occupied {
            reply(connection, status: 503, release: false)
            return
        }
        receiveHeader(connection, buffer: Data(), token: expectedToken, trackID: expectedTrack, file: file!)
    }

    private func receiveHeader(_ connection: NWConnection, buffer: Data, token: String, trackID: Int, file: URL) {
        if buffer.count > 8192 {
            reply(connection, status: 404, release: true)
            return
        }
        connection.receive(minimumIncompleteLength: 1, maximumLength: 8192) { [weak self] data, _, isComplete, error in
            guard let self else { connection.cancel(); return }
            var next = buffer
            if let data { next.append(data) }
            if let range = next.range(of: Data([13, 10, 13, 10])) {
                let head = next.subdata(in: next.startIndex..<range.lowerBound)
                self.handle(connection, header: head, token: token, trackID: trackID, file: file)
                return
            }
            if error != nil || isComplete || next.count > 8192 {
                self.reply(connection, status: 404, release: true)
                return
            }
            self.receiveHeader(connection, buffer: next, token: token, trackID: trackID, file: file)
        }
    }

    private func handle(_ connection: NWConnection, header: Data, token: String, trackID: Int, file: URL) {
        guard let text = String(data: header, encoding: .utf8),
              let line = text.split(separator: "\r\n", maxSplits: 1, omittingEmptySubsequences: false).first else {
            reply(connection, status: 404, release: true)
            return
        }
        let parts = line.split(separator: " ")
        guard parts.count >= 2, parts[0] == "GET",
              let components = URLComponents(string: String(parts[1])),
              components.path == "/xass-lan/\(trackID)" else {
            reply(connection, status: 404, release: true)
            return
        }
        let items = components.queryItems ?? []
        guard items.count == 1, items[0].name == "token", let given = items[0].value, Self.sameToken(given, token) else {
            reply(connection, status: 404, release: true)
            return
        }
        sendFile(connection, file: file)
    }

    private func sendFile(_ connection: NWConnection, file: URL) {
        guard let values = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]),
              values.isRegularFile == true, values.isSymbolicLink != true,
              let size = values.fileSize, size > 0, size <= 256 * 1024 * 1024,
              let header = "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: \(size)\r\nConnection: close\r\nCache-Control: no-store\r\nAccept-Ranges: none\r\n\r\n".data(using: .utf8),
              let handle = try? FileHandle(forReadingFrom: file) else {
            reply(connection, status: 404, release: true)
            return
        }
        connection.send(content: header, completion: .contentProcessed { [weak self] error in
            if error != nil {
                try? handle.close()
                self?.finish(connection, served: false)
                return
            }
            self?.pump(connection, handle: handle, remaining: size)
        })
    }

    private func pump(_ connection: NWConnection, handle: FileHandle, remaining: Int) {
        if remaining == 0 {
            try? handle.close()
            finish(connection, served: true)
            return
        }
        let data = handle.readData(ofLength: min(remaining, 65536))
        guard !data.isEmpty else {
            try? handle.close()
            finish(connection, served: false)
            return
        }
        connection.send(content: data, completion: .contentProcessed { [weak self] error in
            if error != nil {
                try? handle.close()
                self?.finish(connection, served: false)
                return
            }
            self?.pump(connection, handle: handle, remaining: remaining - data.count)
        })
    }

    private func finish(_ connection: NWConnection, served: Bool) {
        connection.cancel()
        lock.lock()
        busy = false
        lock.unlock()
        if served { shutdown() }
    }

    private func reply(_ connection: NWConnection, status: Int, release: Bool) {
        let reason = status == 503 ? "Busy" : "Not Found"
        let body = "HTTP/1.1 \(status) \(reason)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        connection.send(content: Data(body.utf8), completion: .contentProcessed { _ in
            connection.cancel()
        })
        if release {
            lock.lock()
            busy = false
            lock.unlock()
        }
    }

    private static func makeToken() -> String? {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { return nil }
        let token = Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        return token.count >= 32 ? token : nil
    }

    private static func sameToken(_ left: String, _ right: String) -> Bool {
        let a = Array(left.utf8), b = Array(right.utf8)
        guard a.count == b.count, !a.isEmpty else { return false }
        var diff: UInt8 = 0
        for index in a.indices { diff |= a[index] ^ b[index] }
        return diff == 0
    }

    /// Prefer a Wi-Fi RFC1918 address. Loopback and link-local are never advertised.
    static func privateIPv4() -> String? {
        var pointer: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&pointer) == 0, let first = pointer else { return nil }
        defer { freeifaddrs(first) }
        var best: (rank: Int, address: String)?
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let item = cursor {
            defer { cursor = item.pointee.ifa_next }
            let flags = Int32(item.pointee.ifa_flags)
            guard (flags & IFF_UP) != 0, (flags & IFF_LOOPBACK) == 0,
                  let address = item.pointee.ifa_addr, address.pointee.sa_family == UInt8(AF_INET) else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            guard getnameinfo(address, socklen_t(address.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 else { continue }
            let text = String(cString: host)
            guard let rank = rank(text) else { continue }
            let name = item.pointee.ifa_name.map { String(cString: $0) } ?? ""
            let score = rank + (name == "en0" ? 0 : 10)
            if best == nil || score < best!.rank { best = (score, text) }
        }
        return best?.address
    }

    private static func rank(_ text: String) -> Int? {
        let parts = text.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4, parts.allSatisfy({ (0...255).contains($0) }) else { return nil }
        if parts[0] == 192 && parts[1] == 168 { return 0 }
        if parts[0] == 10 { return 1 }
        if parts[0] == 172 && (16...31).contains(parts[1]) { return 2 }
        return nil
    }
}

private final class ResumeOnce<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T?, Never>?
    init(_ continuation: CheckedContinuation<T?, Never>) { self.continuation = continuation }
    @discardableResult func resume(_ value: T?) -> Bool {
        lock.lock()
        let current = continuation
        continuation = nil
        lock.unlock()
        guard let current else { return false }
        current.resume(returning: value)
        return true
    }
}
