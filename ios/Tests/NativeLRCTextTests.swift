import XCTest
@testable import XASS

final class NativeLRCTextTests: XCTestCase {
    private let transcript = "[ar:Нексюша]\n[00:38.16] съел две пачки\n[00:40.00]\n[00:41.50] второй строкой\n[00:45.20] третья <00:46.00>строка"

    func testPlainTextHidesTimestampsPauseRowsAndMetadata() {
        XCTAssertEqual(NativeLRCText.plainText(transcript), "съел две пачки\nвторой строкой\nтретья строка")
        XCTAssertEqual(NativeLRCText.plainText("Обычный текст\nбез тегов"), "Обычный текст\nбез тегов")
        XCTAssertEqual(NativeLRCText.plainText("[01:02.3][01:30.40] припев"), "припев")
    }

    func testEditingSameNumberOfLinesKeepsEveryOriginalTimestamp() {
        let saved = NativeLRCText.applyEdit("съел две пачки фенибута\nвторой строкой\nтретья строка", to: transcript)
        XCTAssertEqual(saved, "[ar:Нексюша]\n[00:38.16] съел две пачки фенибута\n[00:40.00]\n[00:41.50] второй строкой\n[00:45.20] третья строка")
        XCTAssertEqual(NativeLRCText.applyEdit(NativeLRCText.plainText(transcript), to: transcript).components(separatedBy: "\n").count, 5)
    }

    func testAddingOrRemovingLinesStillSavesOrderedValidLRC() {
        let saved = NativeLRCText.applyEdit("одна\nдве\nтри\nчетыре", to: transcript)
        let rows = NativeLRCText.rows(saved).filter { $0.kind == .timed }
        let times = rows.compactMap(\.stamps.first)
        XCTAssertEqual(times, times.sorted(), "Rows stay in time order")
        XCTAssertEqual(times.first ?? -1, 38.16, accuracy: 0.001, "The first line keeps the first original timestamp")
        XCTAssertEqual(rows.filter(\.hasWords).map(\.text), ["одна", "две", "три", "четыре"])
        XCTAssertTrue(rows.contains { !$0.hasWords && $0.stamps.first == 40 }, "Pause rows are kept")
        XCTAssertEqual(NativeLRCText.plainText(saved), "одна\nдве\nтри\nчетыре")

        let shorter = NativeLRCText.applyEdit("только одна", to: transcript)
        XCTAssertTrue(shorter.contains("[00:38.16] только одна"))
    }

    func testOwnerTimestampsOrPlainOriginalAreSavedAsTyped() {
        XCTAssertEqual(NativeLRCText.applyEdit("[00:01.00] своё", to: transcript), "[00:01.00] своё")
        XCTAssertEqual(NativeLRCText.applyEdit("просто текст\n", to: "без таймкодов"), "просто текст")
    }

    func testSyncedLyricsNeverShowRawTags() {
        let value = SyncedLyrics(response: ["lyrics": ["status": "plain", "synced": false, "source": "owner",
            "text": "[00:01.00] первая\n[00:02.00]\n[00:03.00] вторая", "lines": [["start": 1, "end": 2, "text": "[00:01.00] первая"]]]], trackID: 3)
        XCTAssertEqual(value?.text, "первая\nвторая")
        XCTAssertEqual(value?.lines.first?.text, "первая")
    }
}
