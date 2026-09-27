import SwiftUI
import UIKit

/// A small, bounded sample of the actual cover. Missing artwork stays neutral.
struct MusicArtworkPalette: Equatable {
    var red: Double
    var green: Double
    var blue: Double
    static let neutral = MusicArtworkPalette(red: 0.14, green: 0.16, blue: 0.19)
    var color: Color { Color(red: red, green: green, blue: blue) }

    static func sample(_ image: UIImage?) -> MusicArtworkPalette {
        guard let cg = image?.cgImage else { return .neutral }
        var pixels = [UInt8](repeating: 0, count: 8 * 8 * 4)
        let rendered = pixels.withUnsafeMutableBytes { raw -> Bool in
            guard let context = CGContext(data: raw.baseAddress, width: 8, height: 8, bitsPerComponent: 8,
                bytesPerRow: 32, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            context.interpolationQuality = .low
            context.draw(cg, in: CGRect(x: 0, y: 0, width: 8, height: 8)); return true
        }
        guard rendered else { return .neutral }
        var rgb = [Double](repeating: 0, count: 3), weight = 0.0
        for index in stride(from: 0, to: pixels.count, by: 4) {
            let alpha = Double(pixels[index + 3]) / 255
            guard alpha > 0.1 else { continue }
            // Exclude near-black borders; keep restrained, readable colors.
            let values = (0..<3).map { min(1, Double(pixels[index + $0]) / (255 * alpha)) }
            let sampleWeight = max(0.08, (values.max() ?? 0)) * alpha
            for channel in 0..<3 { rgb[channel] += values[channel] * sampleWeight }
            weight += sampleWeight
        }
        guard weight > 0 else { return .neutral }
        let colors = rgb.map { min(0.43, max(0.10, $0 / weight * 0.58)) }
        return MusicArtworkPalette(red: colors[0], green: colors[1], blue: colors[2])
    }
}

@MainActor struct NativeMusicBackdrop: View {
    @ObservedObject var store: NativeStore
    let trackID: Int?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var palette = MusicArtworkPalette.neutral
    var body: some View {
        LinearGradient(colors: [palette.color, palette.color.opacity(0.72), Color(red: 0.055, green: 0.06, blue: 0.075)],
                       startPoint: .topLeading, endPoint: .bottomTrailing)
            .overlay(Color.black.opacity(0.15))
            .ignoresSafeArea()
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.6), value: palette)
            .task(id: trackID) {
                guard let id = trackID else { palette = .neutral; return }
                let image = await store.artwork(id)
                guard !Task.isCancelled else { return }
                palette = MusicArtworkPalette.sample(image)
            }
    }
}

struct MusicPressStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.opacity(configuration.isPressed ? 0.66 : 1)
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.96 : 1)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.16), value: configuration.isPressed)
    }
}

struct MusicCollection: Identifiable, Equatable {
    enum Kind { case album, artist }
    let id: String
    let title: String
    let subtitle: String
    let tracks: [LibraryTrack]
    var artworkID: Int? { tracks.first?.id }
    var duration: Double { tracks.reduce(0) { $0 + $1.duration } }

    static func groups(_ tracks: [LibraryTrack], kind: Kind) -> [MusicCollection] {
        func normalized(_ value: String) -> String {
            value.trimmingCharacters(in: .whitespacesAndNewlines).folding(options: [.caseInsensitive], locale: Locale(identifier: "en_US_POSIX"))
        }
        var order: [String] = [], grouped: [String: [LibraryTrack]] = [:], seen = Set<Int>()
        for track in tracks where seen.insert(track.id).inserted {
            let album = normalized(track.album), artist = normalized(track.artist)
            // Missing album metadata must not invent a giant album named "Unknown".
            if kind == .album && album.isEmpty { continue }
            let key = kind == .album ? "a:\(album.utf8.count):\(album):\(artist)" : "r:\(artist)"
            if grouped[key] == nil { order.append(key) }
            grouped[key, default: []].append(track)
        }
        return order.compactMap { key in
            guard let items = grouped[key], let first = items.first else { return nil }
            let artist = first.artist.trimmingCharacters(in: .whitespacesAndNewlines)
            return MusicCollection(id: key, title: kind == .album ? first.album.trimmingCharacters(in: .whitespacesAndNewlines) : (artist.isEmpty ? "Без исполнителя" : artist),
                subtitle: kind == .album ? (artist.isEmpty ? "Без исполнителя" : artist) : "Треков: \(items.count)", tracks: items)
        }
    }
}
