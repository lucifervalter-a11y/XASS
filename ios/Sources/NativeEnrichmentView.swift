import SwiftUI

enum NativeEnrichmentPresentation {
    static func transcriptProblem(_ text: String) -> String? {
        text.utf8.count > NativeTranscriptPolicy.maximumBytes
            ? "Текст превышает лимит 64 000 байт. Сократите его перед сохранением; ничего не будет обрезано автоматически."
            : nil
    }
    static func message(_ status: String) -> String {
        switch status {
        case "matched": return "Найдены проверенные сведения о песне"
        case "confirmed": return "Подписи подтверждены вами"
        case "candidate", "ambiguous": return "Нужно уточнить совпадение"
        case "disabled": return "Автоматическая замена подписей отключена для этого трека"
        case "unavailable", "rate_limited": return "Каталог временно недоступен. Попробуйте позже"
        case "changed": return "Подписи изменились во время поиска. Повторите поиск"
        case "not_checked": return "Песня ещё не проверена"
        case "insufficient_metadata": return "Для поиска не хватает названия или длительности"
        default: return "Точного совпадения пока нет"
        }
    }
    static func reason(_ info: [String: Any]) -> String? {
        switch info["lookup_reason"] as? String ?? info["reason"] as? String ?? "" {
        case "duration_mismatch": return "Найдена запись другой длительности или у вас отрывок. Можно подтвердить название; таймкоды полной песни к отрывку не применяются."
        case "metadata_needs_confirmation": return "Подписи отличаются от каталога. Выберите совпадение ниже — без подтверждения XASS не заменяет автора и название на предположение."
        case "ambiguous": return "В каталоге несколько версий. Нужен выбор вашей записи."
        case "candidate_changed": return "Запись в каталоге изменилась. Проверьте совпадение перед применением."
        default: return nil
        }
    }
    static func lyricsStatus(_ info: [String: Any]) -> String {
        if info["owner_lyrics_enabled"] as? Bool == true { return "Ваша расшифровка с iPhone" }
        let lyrics = info["lyrics"] as? [String: Any] ?? [:]
        if lyrics["status"] as? String == "instrumental" { return "Инструментальная запись" }
        if let text = lyrics["text"] as? String, !text.isEmpty {
            return lyrics["synced"] as? Bool == true ? "Найден с таймкодами" : "Найден без таймкодов"
        }
        let status = info["lookup_status"] as? String ?? info["status"] as? String ?? "not_checked"
        if ["unavailable", "rate_limited"].contains(status) { return "Каталог не ответил" }
        if ["candidate", "ambiguous"].contains(status) { return "Совпадение не подтверждено" }
        return status == "not_checked" ? "Ещё не проверен" : "Готовый текст не найден"
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
    @AppStorage("xass.transcription.allowNetwork") private var allowNetworkRecognition = false
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
                if let reason = NativeEnrichmentPresentation.reason(info) { Text(reason).font(.footnote).foregroundStyle(.secondary) }
                LabeledContent("Текст песни", value: NativeEnrichmentPresentation.lyricsStatus(info)).font(.callout)
                LabeledContent("Обложка из каталога", value: info["artwork_available"] as? Bool == true ? "Сохранена" : "Не загружена").font(.callout)
                if info["using_cached_result"] as? Bool == true {
                    Text("Каталог временно недоступен. Сохранённые сведения и текст остаются доступны.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if working { ProgressView("Обновляю информацию…") }
                Button("Найти название, обложку и текст") { lookup(refresh: true) }.disabled(working || transcriber.running)
                Text("Поиск по названию, исполнителю и длительности в LRCLIB и MusicBrainz. Это не распознавание аудио по отпечатку: при неверных тегах может потребоваться выбор совпадения. Аудиофайл в каталоги не отправляется.")
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
                                if let album = candidate["album"] as? String, !album.isEmpty { Text(album).font(.caption).foregroundStyle(.secondary) }
                                if let number = candidate["duration"] as? NSNumber { Text(NativeValue.time(number.doubleValue)).font(.caption) }
                            }
                        }.disabled(working || transcriber.running)
                    }
                    Text("Подтверждение исправляет подписи, но не подгоняет таймкоды полной песни под обрезанный файл.").font(.footnote).foregroundStyle(.secondary)
                }
            }
            if info["owner_lyrics_available"] as? Bool == true {
                Section("Какой текст показывать") {
                    Text(info["owner_lyrics_enabled"] as? Bool == true
                         ? "Сейчас в плеере используется сохранённая вами расшифровка. Она имеет приоритет над результатами поиска."
                         : "Сейчас используются готовые слова из файла или каталога. Ваша расшифровка сохранена отдельно.")
                        .font(.callout).foregroundStyle(.secondary)
                    Button(info["owner_lyrics_enabled"] as? Bool == true ? "Использовать готовый текст" : "Вернуть мою расшифровку") {
                        action(path: "/lyrics/source", method: "PATCH", body: ["source": info["owner_lyrics_enabled"] as? Bool == true ? "catalog" : "owner"])
                    }.disabled(working || transcriber.running).accessibilityIdentifier("lyricsSourceToggle")
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
            Text("Без микрофона. По умолчанию аудио остаётся на iPhone (нужна локальная модель языка iOS); облачное распознавание Apple используется только если вы включите его ниже. Проверенный текст сохраняется на вашем сервере. Пение может распознаться с ошибками — проверьте результат.")
                .font(.footnote).foregroundStyle(.secondary)
            Picker("Язык песни", selection: $language) {
                Text("Русский").tag("ru-RU"); Text("English").tag("en-US")
            }.disabled(transcriber.running)
            Toggle("Разрешить распознавание Apple через интернет, если локальной модели нет", isOn: $allowNetworkRecognition)
                .font(.footnote).disabled(transcriber.running).accessibilityIdentifier("transcriptAllowNetwork")
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
                if let problem = NativeEnrichmentPresentation.transcriptProblem(transcript) {
                    Text(problem).font(.footnote).foregroundStyle(.orange).accessibilityIdentifier("transcriptLimit")
                }
                Button("Сохранить проверенный текст") { saveTranscript() }
                    .disabled(working || transcriber.running || NativeEnrichmentPresentation.transcriptProblem(transcript) != nil)
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
    private func action(path: String, method: String = "POST", body: [String: Any]) {
        guard !working, !transcriber.running else { return }; working = true; message = nil
        job = Task {
            defer { working = false }
            do {
                let response = try await store.api.request("/api/mini/music/tracks/\(trackID)" + path, method: method, body: body)
                try store.applyEnrichmentMutationReceipt(response)
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
                let result = try await transcriber.transcribe(file: file, language: language, allowNetwork: allowNetworkRecognition)
                try Task.checkCancellation(); transcript = result
            }
            catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
    private func saveTranscript() {
        guard !working, !transcriber.running else { return }
        if let problem = NativeEnrichmentPresentation.transcriptProblem(transcript) { message = problem; return }
        working = true; message = nil
        job = Task {
            defer { working = false }
            do {
                let response = try await store.api.request("/api/mini/music/tracks/\(trackID)/lyrics", method: "PUT", body: ["text": transcript, "source": "on_device_transcription"])
                try store.applyEnrichmentMutationReceipt(response)
                info["owner_lyrics_available"] = true; info["owner_lyrics_enabled"] = true
                transcript = ""; message = "Текст сохранён. Откройте его в плеере — строки будут следовать реальным таймкодам распознавания."
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
}
