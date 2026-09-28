import Foundation

/// LRC helpers for what the owner SEES and EDITS. Timestamps are never shown
/// ("[00:38.16] слова" is displayed as "слова"), but the text sent to the
/// server keeps them so the player can stay in sync.
enum NativeLRCText {
    enum Kind: Equatable { case meta, timed, plain }
    struct Row: Equatable {
        var kind: Kind
        /// Seconds of every leading `[mm:ss.xx]` tag.
        var stamps: [Double]
        /// Leading tags exactly as written (kept verbatim when a line is edited).
        var prefix: String
        /// Visible words: tags and enhanced-LRC `<mm:ss.xx>` word stamps removed.
        var text: String
        var raw: String
        var hasWords: Bool { kind != .meta && !text.isEmpty }
    }

    private static let stamp = try! NSRegularExpression(pattern: #"\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]"#)
    private static let meta = try! NSRegularExpression(
        pattern: #"^\s*\[(?:ar|ti|al|au|by|re|ve|length|offset|la|id|tool|#):[^\]]*\]\s*$"#, options: [.caseInsensitive])
    private static let wordStamp = try! NSRegularExpression(pattern: #"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>"#)

    static func rows(_ text: String) -> [Row] {
        text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n").map(row)
    }

    static func row(_ line: String) -> Row {
        let value = line as NSString
        if meta.firstMatch(in: line, range: NSRange(location: 0, length: value.length)) != nil {
            return Row(kind: .meta, stamps: [], prefix: line, text: "", raw: line)
        }
        var position = 0, stamps: [Double] = []
        while let match = stamp.firstMatch(in: line, options: [.anchored],
                                           range: NSRange(location: position, length: value.length - position)) {
            let minutes = Double(value.substring(with: match.range(at: 1))) ?? 0
            let seconds = Double(value.substring(with: match.range(at: 2))) ?? 0
            var fraction = 0.0
            if match.range(at: 3).location != NSNotFound { fraction = Double("0." + value.substring(with: match.range(at: 3))) ?? 0 }
            stamps.append(minutes * 60 + seconds + fraction)
            position = match.range.location + match.range.length
        }
        let rest = value.substring(from: position)
        let words = wordStamp.stringByReplacingMatches(in: rest, range: NSRange(location: 0, length: (rest as NSString).length), withTemplate: "")
        let text = words.split(whereSeparator: { $0 == " " || $0 == "\t" }).joined(separator: " ")
        return Row(kind: stamps.isEmpty ? .plain : .timed, stamps: stamps,
                   prefix: value.substring(to: position).trimmingCharacters(in: .whitespaces), text: text, raw: line)
    }

    static func hasTimestamps(_ text: String) -> Bool { rows(text).contains { $0.kind == .timed } }

    /// Only the words, one line per sung phrase; pause rows and tags are hidden.
    static func plainText(_ text: String) -> String {
        guard text.contains("[") || text.contains("<") else { return text }
        return rows(text).filter(\.hasWords).map(\.text).joined(separator: "\n")
    }

    static func format(_ seconds: Double) -> String {
        let centiseconds = max(0, Int((seconds * 100).rounded()))
        return String(format: "[%02d:%02d.%02d]", centiseconds / 6000, centiseconds / 100 % 60, centiseconds % 100)
    }

    /// Puts edited plain lines back into the original LRC.
    /// - Same number of lines: every line keeps its original timestamp exactly
    ///   (pause rows and metadata stay untouched).
    /// - Lines added/removed: the new lines are spread over the original
    ///   timeline (interpolated between the original stamps), so the result is
    ///   still valid, ordered LRC; the original pause rows are kept.
    /// - The owner typed timestamps himself, or the original had none: saved as typed.
    static func applyEdit(_ edited: String, to original: String) -> String {
        let originalRows = rows(original)
        guard originalRows.contains(where: { $0.kind == .timed }), !hasTimestamps(edited) else {
            return edited.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let lines = edited.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
            .map { $0.split(whereSeparator: { $0 == " " || $0 == "\t" }).joined(separator: " ") }
            .filter { !$0.isEmpty }
        let wordRows = originalRows.indices.filter { originalRows[$0].hasWords }
        if lines.count == wordRows.count {
            var result = originalRows.map(\.raw)
            for (index, rowIndex) in wordRows.enumerated() {
                let prefix = originalRows[rowIndex].prefix
                result[rowIndex] = prefix.isEmpty ? lines[index] : prefix + " " + lines[index]
            }
            return result.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let times = wordRows.compactMap { originalRows[$0].stamps.first }
        guard !times.isEmpty else { return lines.joined(separator: "\n") }
        var timed: [(time: Double, order: Int, line: String)] = []
        for (index, line) in lines.enumerated() {
            let position = Double(index) * Double(times.count) / Double(lines.count)
            let slot = min(times.count - 1, Int(position))
            let next = slot + 1 < times.count ? times[slot + 1] : times[slot] + 4
            let time = times[slot] + (position - Double(slot)) * max(0, next - times[slot])
            timed.append((time, 0, format(time) + " " + line))
        }
        for row in originalRows where row.kind == .timed && !row.hasWords {
            if let time = row.stamps.first { timed.append((time, 1, row.raw)) }
        }
        let header = originalRows.filter { $0.kind == .meta }.map(\.raw)
        let body = timed.enumerated().sorted { left, right in
            (left.element.time, left.element.order, left.offset) < (right.element.time, right.element.order, right.offset)
        }.map(\.element.line)
        return (header + body).joined(separator: "\n")
    }
}
