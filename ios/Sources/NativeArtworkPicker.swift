import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import ImageIO
import UIKit

enum NativeArtworkUploadPolicy {
    /// The server accepts eight MiB.  The phone always downscales first and
    /// sends a much smaller JPEG; this source limit also rejects image bombs
    /// and accidental video/document selections before decoding.
    static let maximumSourceBytes = 24 * 1024 * 1024
    static let maximumUploadBytes = 3 * 1024 * 1024
    static let maximumPixelSize = 1800

    static func normalizedJPEG(_ sourceData: Data) throws -> Data {
        guard !sourceData.isEmpty, sourceData.count <= maximumSourceBytes,
              let source = CGImageSourceCreateWithData(sourceData as CFData, nil) else {
            throw OwnerAPIError(status: 0, message: "Выберите изображение не больше 24 МБ.")
        }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maximumPixelSize
        ]
        guard let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
            throw OwnerAPIError(status: 0, message: "Не удалось прочитать это изображение.")
        }
        let image = UIImage(cgImage: thumbnail)
        for quality: CGFloat in [0.90, 0.78, 0.65, 0.52, 0.40] {
            if let data = image.jpegData(compressionQuality: quality), data.count <= maximumUploadBytes {
                return data
            }
        }
        throw OwnerAPIError(status: 0, message: "Обложка слишком сложная. Выберите изображение меньшего размера.")
    }

    static func readImageFile(_ url: URL) throws -> Data {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey, .contentTypeKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true,
              let size = values.fileSize, size > 0, size <= maximumSourceBytes,
              values.contentType?.conforms(to: .image) != false else {
            throw OwnerAPIError(status: 0, message: "Выберите обычный файл изображения не больше 24 МБ.")
        }
        return try Data(contentsOf: url, options: [.mappedIfSafe])
    }
}

/// Owner-only manual cover selection. PhotosUI grants access only to the item
/// selected by the user; Files URLs are security-scoped and read once.
@MainActor struct NativeArtworkPicker: View {
    @ObservedObject var store: NativeStore
    let trackID: Int
    @State private var photo: PhotosPickerItem?
    @State private var selectingFile = false
    @State private var working = false
    @State private var message: String?
    @State private var failed = false

    var body: some View {
        Group {
            PhotosPicker(selection: $photo, matching: .images, preferredItemEncoding: .automatic) {
                Label("Выбрать из Фото", systemImage: "photo.on.rectangle")
            }
            .disabled(working)
            .accessibilityIdentifier("nativeArtworkPhotos")
            Button { selectingFile = true } label: {
                Label("Выбрать из Файлов", systemImage: "folder")
            }
            .disabled(working)
            .accessibilityIdentifier("nativeArtworkFiles")
            if working { ProgressView("Обновляю обложку…") }
            if let message {
                Text(message).font(.footnote).foregroundStyle(failed ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("nativeArtworkMessage")
            }
        }
        .onChange(of: photo) { _, item in
            guard let item else { return }
            working = true; message = nil
            Task {
                do {
                    guard let source = try await item.loadTransferable(type: Data.self) else {
                        throw OwnerAPIError(status: 0, message: "Фото не удалось открыть.")
                    }
                    try await upload(source)
                    photo = nil
                } catch { show(error) }
                working = false
            }
        }
        .fileImporter(isPresented: $selectingFile, allowedContentTypes: [.image]) { result in
            guard case .success(let url) = result else {
                if case .failure(let error) = result { show(error) }
                return
            }
            working = true; message = nil
            Task {
                do {
                    let source = try await Task.detached(priority: .userInitiated) {
                        try NativeArtworkUploadPolicy.readImageFile(url)
                    }.value
                    try await upload(source)
                } catch { show(error) }
                working = false
            }
        }
    }

    private func upload(_ source: Data) async throws {
        let jpeg = try await Task.detached(priority: .userInitiated) {
            try NativeArtworkUploadPolicy.normalizedJPEG(source)
        }.value
        try await store.replaceArtwork(trackID, jpeg: jpeg)
        failed = false; message = "Новая обложка сохранена и появится в плеере и на экране блокировки."
    }

    private func show(_ error: Error) {
        failed = true
        message = error.localizedDescription
    }
}
