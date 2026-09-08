import Foundation
import AVFoundation
import UniformTypeIdentifiers

/// AVPlayer uses our loader instead of following arbitrary HTTP redirects itself.
/// Every request and redirect remains scoped to one validated track on one origin.
final class SecureMediaLoader: NSObject, AVAssetResourceLoaderDelegate, URLSessionDataDelegate {
    private let origin: ServerOrigin
    private let trackID: Int
    private let mediaURL: URL
    private let configuration: URLSessionConfiguration
    private var requests: [Int: Transfer] = [:]
    private lazy var session: URLSession = {
        let config = configuration
        config.httpShouldSetCookies = false
        config.httpCookieStorage = nil
        config.urlCache = nil
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 3600
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()
    private final class Transfer {
        let request: AVAssetResourceLoadingRequest
        let task: URLSessionDataTask
        var responseOffset: Int64 = 0
        init(_ request: AVAssetResourceLoadingRequest, _ task: URLSessionDataTask) {
            self.request = request; self.task = task
        }
    }
    init(origin: ServerOrigin, trackID: Int, url: URL, configuration: URLSessionConfiguration = .ephemeral) {
        self.origin = origin; self.trackID = trackID; mediaURL = url
        self.configuration = configuration
        super.init()
    }
    func asset() -> AVURLAsset {
        let value = AVURLAsset(url: URL(string: "xass-media://track/\(trackID)")!)
        value.resourceLoader.setDelegate(self, queue: .main)
        return value
    }
    func invalidate() {
        for transfer in requests.values { transfer.request.finishLoading(with: URLError(.cancelled)) }
        requests.removeAll(); session.invalidateAndCancel()
    }
    func resourceLoader(_ resourceLoader: AVAssetResourceLoader, shouldWaitForLoadingOfRequestedResource loadingRequest: AVAssetResourceLoadingRequest) -> Bool {
        var request = URLRequest(url: mediaURL)
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        let data = loadingRequest.dataRequest
        let start = max(0, max(data?.currentOffset ?? 0, data?.requestedOffset ?? 0))
        if let data = data, data.requestsAllDataToEndOfResource {
            request.setValue("bytes=\(start)-", forHTTPHeaderField: "Range")
        } else {
            let length = max(1, data?.requestedLength ?? 2)
            guard start <= Int64.max - Int64(length) else { loadingRequest.finishLoading(with: XASSErr.invalidMedia); return true }
            request.setValue("bytes=\(start)-\(start + Int64(length) - 1)", forHTTPHeaderField: "Range")
        }
        let task = session.dataTask(with: request)
        requests[task.taskIdentifier] = Transfer(loadingRequest, task)
        task.resume()
        return true
    }
    func resourceLoader(_ resourceLoader: AVAssetResourceLoader, didCancel loadingRequest: AVAssetResourceLoadingRequest) {
        guard let item = requests.first(where: { $0.value.request === loadingRequest }) else { return }
        requests.removeValue(forKey: item.key); item.value.task.cancel()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, (try? origin.mediaURL(url.absoluteString, trackID: trackID)) != nil else {
            completionHandler(nil)
            finish(task.taskIdentifier, error: XASSErr.invalidMedia)
            return
        }
        completionHandler(request)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse, completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let transfer = requests[dataTask.taskIdentifier], let http = response as? HTTPURLResponse,
              [200, 206].contains(http.statusCode), let url = http.url,
              (try? origin.mediaURL(url.absoluteString, trackID: trackID)) != nil else {
            completionHandler(.cancel); finish(dataTask.taskIdentifier, error: XASSErr.invalidMedia); return
        }
        let mime = http.mimeType ?? "application/octet-stream"
        guard mime.hasPrefix("audio/") || ["application/ogg", "application/octet-stream"].contains(mime) else {
            completionHandler(.cancel); finish(dataTask.taskIdentifier, error: XASSErr.invalidMedia); return
        }
        var length = response.expectedContentLength
        if http.statusCode == 206 {
            guard let range = http.value(forHTTPHeaderField: "Content-Range"),
                  let pieces = Self.parseRange(range) else {
                completionHandler(.cancel); finish(dataTask.taskIdentifier, error: XASSErr.invalidMedia); return
            }
            transfer.responseOffset = pieces.start; length = pieces.total
        }
        if let info = transfer.request.contentInformationRequest {
            info.contentType = UTType(mimeType: mime)?.identifier ?? UTType.audio.identifier
            info.contentLength = max(0, length)
            info.isByteRangeAccessSupported = http.statusCode == 206 || http.value(forHTTPHeaderField: "Accept-Ranges") == "bytes"
        }
        if transfer.request.dataRequest == nil {
            completionHandler(.cancel); finish(dataTask.taskIdentifier, error: nil); return
        }
        completionHandler(.allow)
    }
    static func parseRange(_ header: String) -> (start: Int64, total: Int64)? {
        guard header.hasPrefix("bytes ") else { return nil }
        let components = header.replacingOccurrences(of: "bytes ", with: "").split(separator: "/")
        guard components.count == 2, let total = Int64(components[1]), total > 0,
              let first = components[0].split(separator: "-").first,
              let start = Int64(first), start >= 0, start < total else { return nil }
        return (start, total)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard let transfer = requests[dataTask.taskIdentifier], let target = transfer.request.dataRequest else { return }
        let cursor = max(target.currentOffset, target.requestedOffset)
        let end = transfer.responseOffset + Int64(data.count)
        let skip = max(0, cursor - transfer.responseOffset)
        if end > cursor && skip < data.count {
            var chunk = data.dropFirst(Int(skip))
            if !target.requestsAllDataToEndOfResource {
                let remaining = max(0, target.requestedOffset + Int64(target.requestedLength) - cursor)
                chunk = chunk.prefix(Int(min(remaining, Int64(chunk.count))))
            }
            target.respond(with: Data(chunk))
        }
        transfer.responseOffset = end
        if !target.requestsAllDataToEndOfResource && target.currentOffset >= target.requestedOffset + Int64(target.requestedLength) {
            finish(dataTask.taskIdentifier, error: nil)
        }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) { finish(task.taskIdentifier, error: error) }
    private func finish(_ id: Int, error: Error?) {
        guard let transfer = requests.removeValue(forKey: id) else { return }
        if let error = error { transfer.request.finishLoading(with: error) } else { transfer.request.finishLoading() }
        transfer.task.cancel()
    }
}

final class PrivateDownload: NSObject, URLSessionDownloadDelegate {
    private let origin: ServerOrigin
    private let trackID: Int
    private let destination: URL
    private var completion: ((Result<URL, Error>) -> Void)?
    private var session: URLSession?
    static let maxBytes: Int64 = 256 * 1024 * 1024
    init(origin: ServerOrigin, trackID: Int, destination: URL) {
        self.origin = origin; self.trackID = trackID; self.destination = destination
    }
    func start(url: URL, completion: @escaping (Result<URL, Error>) -> Void) {
        self.completion = completion
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false; config.httpCookieStorage = nil; config.urlCache = nil
        config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 1800
        session = URLSession(configuration: config, delegate: self, delegateQueue: .main)
        session?.downloadTask(with: url).resume()
    }
    func cancel() { complete(.failure(URLError(.cancelled))) }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, (try? origin.mediaURL(url.absoluteString, trackID: trackID)) != nil else {
            completionHandler(nil); complete(.failure(XASSErr.invalidMedia)); return
        }
        completionHandler(request)
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        if totalBytesWritten > Self.maxBytes || totalBytesExpectedToWrite > Self.maxBytes { complete(.failure(URLError(.dataLengthExceedsMaximum))) }
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        guard let response = downloadTask.response as? HTTPURLResponse, response.statusCode == 200,
              let url = response.url, (try? origin.mediaURL(url.absoluteString, trackID: trackID)) != nil,
              let size = try? location.resourceValues(forKeys: [.fileSizeKey]).fileSize,
              size > 0 && Int64(size) <= Self.maxBytes,
              (response.mimeType?.hasPrefix("audio/") == true || response.mimeType == "application/ogg") else {
            complete(.failure(XASSErr.invalidMedia)); return
        }
        do {
            let manager = FileManager.default
            try manager.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            try manager.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: location.path)
            if manager.fileExists(atPath: destination.path) { _ = try manager.replaceItemAt(destination, withItemAt: location) }
            else { try manager.moveItem(at: location, to: destination) }
            var file = destination; var values = URLResourceValues(); values.isExcludedFromBackup = true
            try file.setResourceValues(values)
            complete(.success(destination))
        } catch { complete(.failure(error)) }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error = error { complete(.failure(error)) }
    }
    private func complete(_ result: Result<URL, Error>) {
        guard let done = completion else { return }
        completion = nil; session?.invalidateAndCancel(); session = nil; done(result)
    }
}
