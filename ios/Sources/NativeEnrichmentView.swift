import SwiftUI

enum NativeEnrichmentPresentation {
    static func message(_ status: String) -> String {
        switch status {
        case "matched": return "Найдены проверенные сведения о песне"
        case "confirmed": return "Подписи подтверждены вами"
        case "candidate", "ambiguous": return "Нужно уточнить совпадение"
        case "disabled": return "Автоматическая замена подписей отключена для этого трека"
        case "unavailable", "rate_limited": return "Каталог временно недоступен. Попробуйте позже"
        case "changed": return "Подписи изменились во время поиска. Повторите поиск"
        case "not_checked": return "Песня ещё не проверена"
        default: return "Точного совпадения пока нет"
        }
    }
    static func sourceURL(_ value: Any?) -> URL? {
        guard let string = value as? String, string.count < 1000, let url = URL(string: string),
              url.scheme == "https", url.user == nil, url.password == nil, url.port == nil,
              ["lrclib.net", "musicbrainz.org", "coverartarchive.org"].contains(url.host?.lowercased() ?? "") else { return nil }
        return url
    }
}

@MainActor struct NativeEnrichmentView: View {
    @ObservedObject var store: NativeStore
    let trackID: Int
    @StateObject private var transcriber = NativeTranscription()
    @State private var info: [String: Any] = [:]
    @State private var working = false
    @State private var message: String?
    @State private var transcript = ""
    @State private var language = "ru-RU"
    @State private var job: Task<Void, Never>?
    private var candidates: [[String: Any]] { info["candidates"] as? [[String: Any]] ?? [] }
    private var title: String { store.tracks.first { $0.id == trackID }?.title ?? (store.currentTrack?.id == trackID ? store.currentTrack?.title : nil) ?? "Песня" }
    var body: some View {
        Form {
            Section {
                HStack(spacing: 14) {
                    TrackArtwork(store: store, trackID: trackID).frame(width: 64, height: 64)
                    Text(title).font(.headline)
                }
                Text(NativeEnrichmentPresentation.message(info["status"] as? String ?? "not_checked"))
                if working { ProgressView("Ищу в каталогах…") }
                Button("Найти название, обложку и текст") { lookup(refresh: true) }.disabled(working || transcriber.running)
                Text("При воспроизведении XASS ищет сведения по названию, исполнителю и длительности. Аудиофайл в каталоги не отправляется. Неоднозначные совпадения не применяются сами.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            if !candidates.isEmpty {
                Section("Выберите только точное совпадение") {
                    ForEach(Array(candidates.prefix(3).enumerated()), id: \.offset) { index, candidate in
                        Button {
                            action(path: "/enrichment/confirm", body: ["index": index, "candidate_token": info["candidate_token"] as? String ?? ""])
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(candidate["title"] as? String ?? "Без названия")
                                Text(candidate["artist"] as? String ?? "").font(.subheadline).foregroundStyle(.secondary)
                                if let number = candidate["duration"] as? NSNumber { Text(NativeValue.time(number.doubleValue)).font(.caption) }
                            }
                        }.disabled(working || transcriber.running)
                    }
                    Text("Подтверждение исправляет подписи, но не подгоняет таймкоды полной песни под обрезанный файл.").font(.footnote).foregroundStyle(.secondary)
                }
            }
            transcriptionSection
            if let message = message { Section { Text(message).font(.callout).textSelection(.enabled) } }
            Section("Источники и оригинал") {
                let provenance = info["provenance"] as? [[String: Any]] ?? []
                ForEach(Array(provenance.prefix(4).enumerated()), id: \.offset) { _, item in
                    if let url = NativeEnrichmentPresentation.sourceURL(item["url"]) {
                        Link(item["source"] as? String ?? "Источник", destination: url)
                    }
                }
                Text("Изменяются только сведения в библиотеке XASS. Исходные аудиофайлы и их теги сохраняются.").font(.footnote).foregroundStyle(.secondary)
                if info["can_restore"] as? Bool == true {
                    Button("Вернуть исходные подписи", role: .destructive) { action(path: "/enrichment/restore", body: [:]) }.disabled(working || transcriber.running)
                }
            }
        }.navigationTitle("Информация о песне").navigationBarTitleDisplayMode(.inline)
            .task {
                do {
                    let value = try await store.api.request("/api/mini/music/tracks/\(trackID)/enrichment", method: "GET", body: nil)
                    try Task.checkCancellation(); info = value["enrichment"] as? [String: Any] ?? [:]
                    if info["status"] as? String == "not_checked" { lookup(refresh: false) }
                } catch { if !Task.isCancelled { message = error.localizedDescription } }
            }
            .onDisappear { job?.cancel() }
            .accessibilityIdentifier("nativeEnrichment")
    }

    private var transcriptionSection: some View {
        Section("Если готового текста нет") {
            Text("Расшифровать скачанную песню на iPhone").font(.headline)
            Text("Аудио остаётся на iPhone; проверенный текст сохраняется на вашем сервере. Без микрофона и облачного распознавания. Нужна локальная модель языка в iOS. Пение может распознаться с ошибками — проверьте результат.")
                .font(.footnote).foregroundStyle(.secondary)
            Picker("Язык песни", selection: $language) {
                Text("Русский").tag("ru-RU"); Text("English").tag("en-US")
            }.disabled(transcriber.running)
            if transcriber.running {
                ProgressView(value: transcriber.progress)
                Button("Отменить расшифровку", role: .cancel) { job?.cancel() }
            } else {
                Button("Расшифровать на iPhone") { transcribe() }.disabled(working)
                if let track = store.tracks.first(where: { $0.id == trackID }) ?? store.currentTrack, track.id == trackID,
                   store.audio.analysisFile(trackID) == nil {
                    Button(store.audio.downloadIDs.contains(trackID) ? "Музыка скачивается…" : "Сначала скачать песню") { store.run { try await store.download(track) } }
                        .disabled(store.audio.downloadIDs.contains(trackID))
                }
            }
            if !transcript.isEmpty {
                Text("Автоматическая расшифровка · проверьте слова").font(.caption).foregroundStyle(.orange)
                TextEditor(text: $transcript).frame(minHeight: 200).font(.body).accessibilityIdentifier("transcriptPreview")
                Button("Сохранить проверенный текст") { saveTranscript() }.disabled(working || transcriber.running)
            }
        }
    }
    private func lookup(refresh: Bool) {
        guard !working, !transcriber.running else { return }; working = true; message = nil
        job = Task {
            defer { working = false }
            do {
                let response = try await store.enrichTrack(trackID, refresh: refresh)
                info = response["enrichment"] as? [String: Any] ?? [:]
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
    private func action(path: String, body: [String: Any]) {
        guard !working, !transcriber.running else { return }; working = true; message = nil
        job = Task {
            defer { working = false }
            do {
                let response = try await store.api.request("/api/mini/music/tracks/\(trackID)" + path, method: "POST", body: body)
                try Task.checkCancellation(); store.applyEnrichedTrack(response)
                info = response["enrichment"] as? [String: Any] ?? [:]
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
    private func transcribe() {
        guard !working, !transcriber.running else { return }
        guard let file = store.audio.analysisFile(trackID) else { message = "Сначала скачайте песню на iPhone, затем повторите расшифровку."; return }
        message = nil
        job = Task {
            do {
                let result = try await transcriber.transcribe(file: file, language: language)
                try Task.checkCancellation(); transcript = result
            }
            catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
    private func saveTranscript() {
        guard !working, !transcriber.running, transcript.utf8.count <= 64_000 else { return }; working = true
        job = Task {
            defer { working = false }
            do {
                _ = try await store.api.request("/api/mini/music/tracks/\(trackID)/lyrics", method: "PUT", body: ["text": transcript, "source": "on_device_transcription"])
                try Task.checkCancellation(); transcript = ""; message = "Текст сохранён. Откройте его в плеере — строки будут следовать реальным таймкодам распознавания."
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
}
