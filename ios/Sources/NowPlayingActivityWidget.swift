import SwiftUI
import WidgetKit
#if canImport(ActivityKit)
import ActivityKit

/// Attributes for Now Playing Live Activity & Dynamic Island.
public struct NowPlayingAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        public var title: String
        public var artist: String
        public var duration: Double
        public var position: Double
        public var isPlaying: Bool
        public var deviceName: String

        public init(
            title: String,
            artist: String,
            duration: Double,
            position: Double,
            isPlaying: Bool,
            deviceName: String
        ) {
            self.title = title
            self.artist = artist
            self.duration = duration
            self.position = position
            self.isPlaying = isPlaying
            self.deviceName = deviceName
        }
    }

    public var trackID: String
    public init(trackID: String) {
        self.trackID = trackID
    }
}

/// Dynamic Island & Lock Screen Widget presentation.
public struct NowPlayingActivityWidgetView: View {
    let context: ActivityViewContext<NowPlayingAttributes>

    public var body: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color(red: 22 / 255, green: 25 / 255, blue: 30 / 255))
                Image(systemName: "music.note")
                    .font(.title3)
                    .foregroundStyle(.white.opacity(0.8))
            }
            .frame(width: 48, height: 48)

            VStack(alignment: .leading, spacing: 3) {
                Text(context.state.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                Text(context.state.artist.isEmpty ? context.state.deviceName : context.state.artist)
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.65))
                    .lineLimit(1)
            }

            Spacer(minLength: 0)

            Image(systemName: context.state.isPlaying ? "waveform" : "pause.fill")
                .foregroundStyle(Color(red: 52 / 255, green: 120 / 255, blue: 246 / 255))
                .font(.body)
        }
        .padding(14)
        .background(Color.black)
    }
}

#if os(iOS)
public struct NowPlayingLiveActivity: Widget {
    public init() {}

    public var body: some WidgetConfiguration {
        ActivityConfiguration(for: NowPlayingAttributes.self) { context in
            NowPlayingActivityWidgetView(context: context)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 6) {
                        Image(systemName: "music.note")
                            .foregroundStyle(Color(red: 52 / 255, green: 120 / 255, blue: 246 / 255))
                        Text(context.state.title)
                            .font(.headline)
                            .lineLimit(1)
                    }
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Image(systemName: context.state.isPlaying ? "waveform" : "pause.fill")
                        .foregroundStyle(Color(red: 52 / 255, green: 120 / 255, blue: 246 / 255))
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(context.state.artist.isEmpty ? context.state.deviceName : context.state.artist)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        ProgressView(value: min(max(context.state.duration > 0 ? context.state.position / context.state.duration : 0, 0), 1))
                            .tint(Color(red: 52 / 255, green: 120 / 255, blue: 246 / 255))
                    }
                    .padding(.top, 4)
                }
            } compactLeading: {
                Image(systemName: "music.note")
                    .foregroundStyle(Color(red: 52 / 255, green: 120 / 255, blue: 246 / 255))
            } compactTrailing: {
                Image(systemName: context.state.isPlaying ? "waveform" : "pause.fill")
                    .font(.caption2)
                    .foregroundStyle(Color(red: 52 / 255, green: 120 / 255, blue: 246 / 255))
            } minimal: {
                Image(systemName: "music.note")
                    .foregroundStyle(Color(red: 52 / 255, green: 120 / 255, blue: 246 / 255))
            }
        }
    }
}
#endif
#endif
