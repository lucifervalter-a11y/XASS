import Foundation
import ActivityKit

// Compiled into both the app and its WidgetKit extension. Keep only the Codable
// contract here: the extension must not link app services or a second @main.
public struct NowPlayingAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        public var title: String
        public var artist: String
        public var duration: Double
        public var position: Double
        public var isPlaying: Bool
        public var deviceName: String

        public init(title: String, artist: String, duration: Double, position: Double, isPlaying: Bool, deviceName: String) {
            self.title = title
            self.artist = artist
            self.duration = duration
            self.position = position
            self.isPlaying = isPlaying
            self.deviceName = deviceName
        }
    }

    public var trackID: String
    public init(trackID: String) { self.trackID = trackID }
}

public struct HermesActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable, Sendable {
        public var queryPreview: String
        public var statusPhase: String
        public var progress: Double
        public var isThinking: Bool
        public var responseTokens: Int
        public var stepDescription: String

        public init(queryPreview: String, statusPhase: String, progress: Double = 0.0,
                    isThinking: Bool = true, responseTokens: Int = 0, stepDescription: String = "") {
            self.queryPreview = queryPreview
            self.statusPhase = statusPhase
            self.progress = progress
            self.isThinking = isThinking
            self.responseTokens = responseTokens
            self.stepDescription = stepDescription
        }
    }

    public var sessionID: String
    public var sessionTitle: String
    public init(sessionID: String, sessionTitle: String) {
        self.sessionID = sessionID
        self.sessionTitle = sessionTitle
    }
}
