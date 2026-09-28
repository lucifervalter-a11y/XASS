#if DEBUG
import SwiftUI
import UIKit

/// Deterministic, timer-driven player for SwiftUI Previews and simulator UI
/// fixtures. No audio, network, session or device commands. Lyrics and art
/// are labelled generated test content, not real songs.
@MainActor final class FixturePlayerState: PlayerStateProviding {
    struct Item {
        let track: PlayerTrack
        let duration: TimeInterval
        let lyrics: TimedLyrics?
    }

    let items: [Item]
    @Published private(set) var index: Int
    @Published private(set) var isPlaying: Bool
    @Published private(set) var position: TimeInterval
    @Published private(set) var volume: Double = 0.62
    @Published private(set) var currentLyricIndex: Int? = nil
    var track: PlayerTrack? { items.indices.contains(index) ? items[index].track : nil }
    var duration: TimeInterval { items.indices.contains(index) ? items[index].duration : 0 }
    var lyrics: TimedLyrics? { items.indices.contains(index) ? items[index].lyrics : nil }
    private var timer: Timer?
    private let tick: TimeInterval = 0.25

    init(trackIndex: Int = 0, position: TimeInterval = 0, playing: Bool = true, items: [Item]? = nil) {
        let list = items ?? Self.makeItems()
        self.items = list
        index = min(max(0, trackIndex), max(0, list.count - 1))
        self.position = position
        isPlaying = playing
        updateLyricIndex()
    }

    /// Screen presets for `--native-ui-screen nowplaying|lyrics|miniplayer`.
    func applyFixtureScreen(_ screen: String) {
        index = 0
        position = screen == "miniplayer" ? 12 : 41
        isPlaying = true
        updateLyricIndex()
        start()
    }

    func start() {
        guard timer == nil else { return }
        let timer = Timer(timeInterval: tick, repeats: true) { [weak self] timer in
            MainActor.assumeIsolated {
                guard let self = self else { timer.invalidate(); return }
                self.advance()
            }
        }
        // .common keeps lyrics moving while the user scrolls.
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() { timer?.invalidate(); timer = nil }

    private func advance() {
        guard isPlaying else { return }
        let next = position + tick
        if next >= duration { self.next(); return }
        position = next
        updateLyricIndex()
    }

    private func updateLyricIndex() {
        let value = lyrics?.index(at: position)
        if value != currentLyricIndex { currentLyricIndex = value }
    }

    func play() { isPlaying = true }
    func pause() { isPlaying = false }
    func next() {
        guard !items.isEmpty else { return }
        index = (index + 1) % items.count
        position = 0
        updateLyricIndex()
    }
    func previous() {
        guard !items.isEmpty else { return }
        if position > 3 { position = 0 } else { index = (index - 1 + items.count) % items.count; position = 0 }
        updateLyricIndex()
    }
    func seek(to position: TimeInterval) {
        guard position.isFinite else { return }
        self.position = min(max(0, position), max(0, duration - 0.5))
        updateLyricIndex()
    }
    func setVolume(_ value: Double) {
        guard value.isFinite else { return }
        volume = min(1, max(0, value))
    }

    // MARK: - Generated fixture content

    static func makeItems() -> [Item] {
        let first = [
            "Проверка синхронного текста", "Первая строка идёт по таймеру", "Город молчит под фонарями",
            "Мы едем по пустым мостам", "Каждая строка ждёт свою секунду", "И подсвечивается вовремя",
            "Коснись строки — и песня перейдёт", "Прямо к этому месту", "Прокрути вручную вверх и вниз",
            "Через пару секунд текст вернётся", "Сам к текущей строке", "Без рывков и без мигания",
            "Тихий город, тихий свет", "Ночной маршрут без остановок", "Тестовый припев, строка один",
            "Тестовый припев, строка два", "Тёплый ветер в открытое окно", "Фары рисуют длинные линии",
            "Здесь могла быть твоя песня", "Но это только проверка", "Предпоследний куплет", "Почти приехали",
            "Последний поворот налево", "Конец тестового фрагмента"
        ]
        let second = [
            "После дождя всё звучит иначе", "Капли стучат по стеклу в такт", "Вторая песня, другой цвет",
            "Фон плавно меняет оттенок", "Без вспышек и без скачков", "Строки снова ждут свою очередь",
            "Лужи отражают небо", "Тестовый куплет продолжается", "Смахни обложку влево", "И перейди к следующей",
            "Или вправо — к предыдущей", "Проверка анимации", "Мягкая пружина", "Ничего не прыгает",
            "Серый асфальт становится синим", "Снова тестовый припев", "Снова тестовый припев, ещё раз",
            "Солнце выходит из-за туч", "Последние капли", "Конец второго фрагмента"
        ]
        let fourth = [
            "Последний поезд уходит в ночь", "Вагоны качаются мерно", "Четвёртая тестовая песня",
            "Пустой перрон и жёлтый свет", "Колёса отбивают ритм", "Строка за строкой", "Станция за станцией",
            "Тестовый текст не заканчивается", "Пока не кончится трек", "За окном мелькают огни",
            "Кто-то спит, а кто-то слушает", "Проверка длинной строки, которая переносится на следующую линию экрана",
            "И снова короткая", "Поезд замедляется", "Скоро конечная", "Двери открываются",
            "Пора выходить", "Конец четвёртого фрагмента"
        ]
        func timed(_ rows: [String], start: TimeInterval, step: TimeInterval, duration: TimeInterval) -> TimedLyrics {
            TimedLyrics(starts: rows.enumerated().map { (start + Double($0.offset) * step, $0.element) }, duration: duration)
        }
        return [
            Item(track: PlayerTrack(id: "fixture-1", title: "Тихий город", artist: "Тестовая библиотека",
                                    artworkImage: artwork(top: UIColor(red: 0.10, green: 0.32, blue: 0.62, alpha: 1), bottom: UIColor(red: 0.05, green: 0.66, blue: 0.64, alpha: 1), label: "01")),
                 duration: 138, lyrics: timed(first, start: 3, step: 5, duration: 138)),
            Item(track: PlayerTrack(id: "fixture-2", title: "После дождя", artist: "Тестовая библиотека",
                                    artworkImage: artwork(top: UIColor(red: 0.78, green: 0.20, blue: 0.42, alpha: 1), bottom: UIColor(red: 0.98, green: 0.58, blue: 0.22, alpha: 1), label: "02")),
                 duration: 124, lyrics: timed(second, start: 2, step: 5.5, duration: 124)),
            Item(track: PlayerTrack(id: "fixture-3", title: "Дорога домой", artist: "Тестовая коллекция",
                                    artworkImage: artwork(top: UIColor(red: 0.16, green: 0.46, blue: 0.20, alpha: 1), bottom: UIColor(red: 0.86, green: 0.80, blue: 0.28, alpha: 1), label: "03")),
                 duration: 96, lyrics: nil),
            Item(track: PlayerTrack(id: "fixture-4", title: "Последний поезд", artist: "Тестовая коллекция",
                                    artworkImage: artwork(top: UIColor(red: 0.30, green: 0.14, blue: 0.56, alpha: 1), bottom: UIColor(red: 0.86, green: 0.22, blue: 0.30, alpha: 1), label: "04")),
                 duration: 112, lyrics: timed(fourth, start: 4, step: 5.5, duration: 112))
        ]
    }

    static func artwork(top: UIColor, bottom: UIColor, label: String) -> UIImage {
        let size = CGSize(width: 600, height: 600)
        return UIGraphicsImageRenderer(size: size).image { context in
            let cg = context.cgContext
            let colors = [top.cgColor, bottom.cgColor] as CFArray
            if let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1]) {
                cg.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: size.width, y: size.height), options: [])
            }
            for ring in 0..<7 {
                let inset = CGFloat(ring) * 38 + 60
                UIColor.white.withAlphaComponent(0.06 + CGFloat(ring) * 0.015).setStroke()
                let path = UIBezierPath(ovalIn: CGRect(x: inset, y: inset, width: size.width - inset * 2, height: size.height - inset * 2))
                path.lineWidth = 10; path.stroke()
            }
            (label as NSString).draw(at: CGPoint(x: 40, y: 32), withAttributes: [.font: UIFont.systemFont(ofSize: 96, weight: .heavy), .foregroundColor: UIColor.white.withAlphaComponent(0.9)])
            ("XASS · TEST ART" as NSString).draw(at: CGPoint(x: 40, y: 548), withAttributes: [.font: UIFont.systemFont(ofSize: 22, weight: .semibold), .foregroundColor: UIColor.white.withAlphaComponent(0.85)])
        }
    }
}
#endif
