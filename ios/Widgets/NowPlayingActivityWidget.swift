import SwiftUI
import WidgetKit
#if canImport(ActivityKit)
import ActivityKit

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

public struct XASSWidgetEntry: TimelineEntry {
    public let date: Date
    public let title: String
    public let artist: String
    public let isPlaying: Bool
    public let serverName: String
    public init(date: Date, title: String, artist: String, isPlaying: Bool, serverName: String) {
        self.date = date; self.title = title; self.artist = artist; self.isPlaying = isPlaying; self.serverName = serverName
    }
}

public struct XASSWidgetProvider: TimelineProvider {
    public init() {}
    public func placeholder(in context: Context) -> XASSWidgetEntry {
        XASSWidgetEntry(date: Date(), title: "Midnight City", artist: "M83", isPlaying: true, serverName: "xass.dev")
    }
    public func getSnapshot(in context: Context, completion: @escaping (XASSWidgetEntry) -> Void) {
        completion(placeholder(in: context))
    }
    public func getTimeline(in context: Context, completion: @escaping (Timeline<XASSWidgetEntry>) -> Void) {
        let entry = XASSWidgetEntry(date: Date(), title: "Музыка XASS", artist: "Личный сервер", isPlaying: false, serverName: "XASS")
        let timeline = Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(30 * 60)))
        completion(timeline)
    }
}

public struct XASSHomeWidgetView: View {
    let entry: XASSWidgetEntry
    @Environment(\.widgetFamily) var family

    public var body: some View {
        ZStack {
            Color(red: 10/255, green: 14/255, blue: 20/255)
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Image(systemName: "waveform")
                        .foregroundStyle(Color(red: 52/255, green: 120/255, blue: 246/255))
                    Text("XASS")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.white.opacity(0.7))
                    Spacer()
                    if entry.isPlaying {
                        Circle().fill(Color.green).frame(width: 6, height: 6)
                    }
                }
                Spacer(minLength: 0)
                Text(entry.title)
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.white)
                    .lineLimit(family == .systemSmall ? 2 : 1)
                Text(entry.artist)
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.65))
                    .lineLimit(1)
            }
            .padding(14)
        }
    }
}

public struct XASSHomeWidget: Widget {
    public let kind: String = "XASSHomeWidget"
    public init() {}
    public var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: XASSWidgetProvider()) { entry in
            XASSHomeWidgetView(entry: entry)
                .containerBackground(Color(red: 10 / 255, green: 14 / 255, blue: 20 / 255), for: .widget)
        }
        .configurationDisplayName("XASS Музыка")
        .description("Сейчас играет и статус сервера XASS")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

public struct HermesActivityWidgetView: View {
    let context: ActivityViewContext<HermesActivityAttributes>

    public var body: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color(red: 21 / 255, green: 27 / 255, blue: 38 / 255))
                Image(systemName: "sparkles")
                    .font(.title3)
                    .foregroundStyle(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
            }
            .frame(width: 44, height: 44)

            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text("XASS AI")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.white)
                    Spacer()
                    Text(context.state.statusPhase)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
                }

                Text(context.state.stepDescription.isEmpty ? context.state.queryPreview : context.state.stepDescription)
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.7))
                    .lineLimit(1)

                ProgressView(value: context.state.progress)
                    .tint(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
            }
        }
        .padding(14)
        .background(Color(red: 10 / 255, green: 13 / 255, blue: 20 / 255))
    }
}

public struct HermesLiveActivity: Widget {
    public init() {}

    public var body: some WidgetConfiguration {
        ActivityConfiguration(for: HermesActivityAttributes.self) { context in
            HermesActivityWidgetView(context: context)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 6) {
                        Image(systemName: "sparkles")
                            .foregroundStyle(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
                        Text("XASS AI")
                            .font(.headline)
                    }
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.state.statusPhase)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(context.state.stepDescription.isEmpty ? context.state.queryPreview : context.state.stepDescription)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        ProgressView(value: context.state.progress)
                            .tint(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
                    }
                    .padding(.top, 4)
                }
            } compactLeading: {
                Image(systemName: "sparkles")
                    .foregroundStyle(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
            } compactTrailing: {
                if context.state.isThinking {
                    Image(systemName: "ellipsis")
                        .font(.caption2)
                        .foregroundStyle(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
                } else {
                    Image(systemName: "checkmark")
                        .font(.caption2)
                        .foregroundStyle(.green)
                }
            } minimal: {
                Image(systemName: "sparkles")
                    .foregroundStyle(Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255))
            }
        }
    }
}

@main
public struct XASSWidgetBundle: WidgetBundle {
    public init() {}
    public var body: some Widget {
        NowPlayingLiveActivity()
        XASSHomeWidget()
        HermesLiveActivity()
    }
}

#endif
