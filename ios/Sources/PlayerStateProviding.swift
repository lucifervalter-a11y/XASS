import SwiftUI
import UIKit

/// One timed lyric row. `end` is where the next row starts (or the track ends).
struct TimedLyricLine: Equatable {
    var start: TimeInterval
    var end: TimeInterval
    var text: String
    /// Optional verified translation supplied with this exact timed row.
    /// XASS never guesses that a simultaneous duet line is a translation.
    var translation: String?
    /// Optional provider pronunciation. When absent the UI may offer an
    /// on-device Foundation transliteration without sending lyrics anywhere.
    var pronunciation: String?

    init(start: TimeInterval, end: TimeInterval, text: String,
         translation: String? = nil, pronunciation: String? = nil) {
        self.start = start; self.end = end; self.text = text
        self.translation = translation; self.pronunciation = pronunciation
    }
}

struct TimedLyrics: Equatable {
    var lines: [TimedLyricLine]

    var hasTranslation: Bool {
        lines.contains { $0.translation?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false }
    }
    var hasPronunciation: Bool {
        lines.contains {
            $0.pronunciation?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false ||
                $0.text.unicodeScalars.contains { scalar in
                    guard CharacterSet.letters.contains(scalar) else { return false }
                    return !(65...90).contains(scalar.value) && !(97...122).contains(scalar.value)
                }
        }
    }

    init(lines: [TimedLyricLine]) { self.lines = lines }

    /// Builds rows from start times; every row ends where the next begins.
    init(starts: [(TimeInterval, String)], duration: TimeInterval) {
        let sorted = starts.filter { $0.0.isFinite && $0.0 >= 0 }.sorted { $0.0 < $1.0 }
        lines = sorted.enumerated().map { offset, row in
            let end = offset + 1 < sorted.count ? sorted[offset + 1].0 : max(row.0, duration)
            return TimedLyricLine(start: row.0, end: end, text: row.1)
        }
    }

    /// The row being sung at `position`, nil before the first row.
    /// Binary search: providers call this at playback cadence, never a view body.
    func index(at position: TimeInterval) -> Int? {
        guard position.isFinite, !lines.isEmpty else { return nil }
        var lower = 0, upper = lines.count
        while lower < upper {
            let middle = (lower + upper) / 2
            if lines[middle].start <= position { lower = middle + 1 } else { upper = middle }
        }
        return lower > 0 ? lower - 1 : nil
    }

    /// The vocal row immediately before a real instrumental gap. A gap is
    /// deliberately separate from `index(at:)`: during it no lyric is marked
    /// as sung and the player can show the familiar animated ellipsis.
    func pauseAfterIndex(at position: TimeInterval, minimumGap: TimeInterval = 1.5) -> Int? {
        guard position.isFinite, minimumGap.isFinite, minimumGap >= 0, lines.count > 1 else { return nil }
        var lower = 0, upper = lines.count
        while lower < upper {
            let middle = (lower + upper) / 2
            if lines[middle].start <= position { lower = middle + 1 } else { upper = middle }
        }
        let previous = lower - 1
        guard lines.indices.contains(previous), lines.indices.contains(previous + 1) else { return nil }
        let gapStart = max(lines[previous].start, lines[previous].end)
        let gapEnd = lines[previous + 1].start
        guard gapEnd - gapStart >= minimumGap, position >= gapStart, position < gapEnd else { return nil }
        return previous
    }
}

/// What the player UI shows about the current song. Artwork is either an
/// in-memory image or a local file URL; remote artwork must be resolved by the
/// provider (for example through SecureMediaLoader) before it reaches the UI.
struct PlayerTrack: Identifiable, Equatable {
    let id: String
    var title: String
    var artist: String
    var artworkURL: URL?
    var artworkImage: UIImage?

    init(id: String, title: String, artist: String, artworkURL: URL? = nil, artworkImage: UIImage? = nil) {
        self.id = id; self.title = title; self.artist = artist
        self.artworkURL = artworkURL; self.artworkImage = artworkImage
    }

    static func == (lhs: PlayerTrack, rhs: PlayerTrack) -> Bool {
        lhs.id == rhs.id && lhs.title == rhs.title && lhs.artist == rhs.artist
            && lhs.artworkURL == rhs.artworkURL && lhs.artworkImage === rhs.artworkImage
    }
}

/// The only thing the Apple Music-style player UI talks to. Real playback
/// (NativeStore/AudioController) and the UI fixture both conform, so the views
/// never change when the wiring does.
@MainActor protocol PlayerStateProviding: ObservableObject {
    var track: PlayerTrack? { get }
    var isPlaying: Bool { get }
    var position: TimeInterval { get }
    var duration: TimeInterval { get }
    /// 0...1
    var volume: Double { get }
    var lyrics: TimedLyrics? { get }
    var currentLyricIndex: Int? { get }
    /// A command is in flight; transport controls are disabled. Defaults to false.
    var isBusy: Bool { get }
    func play()
    func pause()
    func next()
    func previous()
    func seek(to position: TimeInterval)
    func setVolume(_ value: Double)
}

extension PlayerStateProviding {
    var isBusy: Bool { false }
    var progress: Double { duration > 0 ? min(1, max(0, position / duration)) : 0 }
    func togglePlayPause() { if isPlaying { pause() } else { play() } }
}

/// Exactly one player-level overlay is visible at a time. The root shell derives
/// this from explicit state instead of stacking sheets, so the mini player,
/// Now Playing, the device picker and the transfer banner never overlap.
enum NativeRootOverlay: Equatable {
    case hidden
    case miniPlayer
    case nowPlaying
    /// Route/device picker (today's NativeRoutePicker sheet, or the AirPlay-style
    /// picker from `ux/device-picker-remote`). Hides the whole player layer.
    case devicePicker
    case transferBanner
    /// Login / enrollment sheet: highest priority, the whole player layer yields.
    case accountSheet

    /// Priority: account sheet > picker > Now Playing > transfer banner > mini player.
    /// While Now Playing is open, transfer progress is shown inside it.
    static func resolve(hasTrack: Bool, expanded: Bool, devicePicker: Bool, transferActive: Bool,
                        accountSheet: Bool = false) -> NativeRootOverlay {
        if accountSheet { return .accountSheet }
        if devicePicker { return .devicePicker }
        if expanded && hasTrack { return .nowPlaying }
        if transferActive { return .transferBanner }
        return hasTrack ? .miniPlayer : .hidden
    }

}
