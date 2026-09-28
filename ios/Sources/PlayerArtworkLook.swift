import SwiftUI
import UIKit
import CoreImage

/// Background look for one artwork: a few dominant colors (darkened for white
/// text) and a small pre-blurred bitmap. Computed once per artwork off the
/// main thread and cached, so Now Playing never blurs live in `body`.
struct PlayerArtworkLook: Equatable, @unchecked Sendable {
    let key: String
    let colors: [Color]
    let blurred: UIImage?

    static let neutral = PlayerArtworkLook(key: "neutral", colors: [
        Color(red: 0.17, green: 0.19, blue: 0.24), Color(red: 0.11, green: 0.12, blue: 0.16), Color(red: 0.05, green: 0.05, blue: 0.07)
    ], blurred: nil)

    static func == (lhs: PlayerArtworkLook, rhs: PlayerArtworkLook) -> Bool { lhs.key == rhs.key }

    /// Keyed by the image instance: while a provider still shows the previous
    /// cover for a new song, the background keeps its colors (no flash).
    static func cacheKey(trackID: String, image: UIImage?) -> String {
        guard let image = image else { return "none-\(trackID)" }
        return "image-\(ObjectIdentifier(image).hashValue)"
    }
}

@MainActor final class PlayerArtworkLookCache {
    static let shared = PlayerArtworkLookCache()
    private var cache: [String: PlayerArtworkLook] = [:]
    private var order: [String] = []
    private var running: [String: Task<PlayerArtworkLook, Never>] = [:]
    /// Keeps keyed images alive so an ObjectIdentifier is never reused while cached.
    private var pinned: [String: UIImage] = [:]
    private let limit = 24

    func cached(_ key: String) -> PlayerArtworkLook? { cache[key] }

    func look(key: String, image: UIImage?) async -> PlayerArtworkLook {
        if let hit = cache[key] { return hit }
        guard let cgImage = image?.cgImage else { return .neutral }
        if let task = running[key] { return await task.value }
        let task = Task.detached(priority: .userInitiated) { PlayerArtworkProcessing.compute(cgImage, key: key) }
        running[key] = task
        let look = await task.value
        running[key] = nil
        cache[key] = look
        pinned[key] = image
        order.removeAll { $0 == key }; order.append(key)
        while order.count > limit { let evicted = order.removeFirst(); cache[evicted] = nil; pinned[evicted] = nil }
        return look
    }
}

/// Pure image work, free of any actor so it runs on a background task.
enum PlayerArtworkProcessing {
    private static let context = CIContext(options: [.useSoftwareRenderer: false, .cacheIntermediates: false])

    static func compute(_ image: CGImage, key: String) -> PlayerArtworkLook {
        PlayerArtworkLook(key: key, colors: dominantColors(image), blurred: blurred(image))
    }

    /// Quantized histogram over a 24×24 thumbnail, weighted toward saturated
    /// pixels. Returns three distinct colors, dimmed so white text stays readable.
    static func dominantColors(_ image: CGImage) -> [Color] {
        let side = 24
        var pixels = [UInt8](repeating: 0, count: side * side * 4)
        let drawn = pixels.withUnsafeMutableBytes { raw -> Bool in
            guard let context = CGContext(data: raw.baseAddress, width: side, height: side, bitsPerComponent: 8, bytesPerRow: side * 4,
                                          space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            context.interpolationQuality = .medium
            context.draw(image, in: CGRect(x: 0, y: 0, width: side, height: side))
            return true
        }
        guard drawn else { return PlayerArtworkLook.neutral.colors }
        var sums = [Int: (r: Double, g: Double, b: Double, w: Double)]()
        for offset in stride(from: 0, to: pixels.count, by: 4) {
            let alpha = Double(pixels[offset + 3]) / 255
            guard alpha > 0.2 else { continue }
            let r = Double(pixels[offset]) / 255 / alpha, g = Double(pixels[offset + 1]) / 255 / alpha, b = Double(pixels[offset + 2]) / 255 / alpha
            let maxValue = max(r, g, b), minValue = min(r, g, b)
            let saturation = maxValue > 0 ? (maxValue - minValue) / maxValue : 0
            let weight = (0.15 + saturation) * (maxValue < 0.08 ? 0.2 : 1) * alpha
            let bucket = Int(min(3, r * 4)) * 16 + Int(min(3, g * 4)) * 4 + Int(min(3, b * 4))
            let old = sums[bucket] ?? (0, 0, 0, 0)
            sums[bucket] = (old.r + r * weight, old.g + g * weight, old.b + b * weight, old.w + weight)
        }
        var picked: [(Double, Double, Double)] = []
        for entry in sums.values.sorted(by: { $0.w > $1.w }) where entry.w > 0 {
            let color = (entry.r / entry.w, entry.g / entry.w, entry.b / entry.w)
            let distinct = picked.allSatisfy { abs($0.0 - color.0) + abs($0.1 - color.1) + abs($0.2 - color.2) > 0.25 }
            if distinct { picked.append(color) }
            if picked.count == 3 { break }
        }
        guard let first = picked.first else { return PlayerArtworkLook.neutral.colors }
        while picked.count < 3 { let last = picked.last ?? first; picked.append((last.0 * 0.6, last.1 * 0.6, last.2 * 0.6)) }
        return picked.enumerated().map { index, rgb in
            var hue: CGFloat = 0, saturation: CGFloat = 0, brightness: CGFloat = 0, alpha: CGFloat = 0
            UIColor(red: rgb.0, green: rgb.1, blue: rgb.2, alpha: 1).getHue(&hue, saturation: &saturation, brightness: &brightness, alpha: &alpha)
            // Top of the screen a little brighter, bottom darker; always readable under white text.
            let ceiling: CGFloat = [0.62, 0.48, 0.32][index]
            return Color(hue: Double(hue), saturation: Double(min(0.85, saturation * 1.1)), brightness: Double(min(ceiling, max(0.16, brightness * 0.8))))
        }
    }

    /// Downsample first, then blur: a 96px Gaussian is cheap and looks the same
    /// as a full-size blur once stretched behind the screen.
    static func blurred(_ image: CGImage) -> UIImage? {
        let input = CIImage(cgImage: image)
        let scale = 96 / max(1, max(input.extent.width, input.extent.height))
        let small = input.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        guard let filter = CIFilter(name: "CIGaussianBlur") else { return nil }
        filter.setValue(small.clampedToExtent(), forKey: kCIInputImageKey)
        filter.setValue(9, forKey: kCIInputRadiusKey)
        guard let output = filter.outputImage?.cropped(to: small.extent),
              let rendered = context.createCGImage(output, from: small.extent) else { return nil }
        return UIImage(cgImage: rendered)
    }
}
