import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// The document picker has to be presented by a normal controller. As the root
/// of a SwiftUI cover, Open never calls the delegate and Files stays on screen.
@MainActor final class NativeMusicPickerHost: UIViewController {
    var wantsPicker = false
    var makePicker: (() -> UIDocumentPickerViewController)?
    private var started = false
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        presentPickerIfNeeded()
    }
    func presentPickerIfNeeded() {
        guard wantsPicker, !started, presentedViewController == nil, let makePicker else { return }
        started = true
        present(makePicker(), animated: true)
    }
    func resetIfIdle() {
        if presentedViewController == nil { started = false }
    }
}

/// Opens in place: Files must not silently copy an entire archive before it
/// delivers the selection. The import engine coordinates its own bounded copy.
@MainActor struct NativeMusicDocumentPicker: UIViewControllerRepresentable {
    @Binding var presented: Bool
    var selected: ([URL]) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    static func openingController(delegate: UIDocumentPickerDelegate) -> UIDocumentPickerViewController {
        var types: [UTType] = [.audio, .zip]
        for ext in ["mp3", "m4a", "wav", "flac", "ogg"] {
            if let type = UTType(filenameExtension: ext), !types.contains(type) { types.append(type) }
        }
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: types, asCopy: false)
        picker.allowsMultipleSelection = true
        picker.delegate = delegate
        picker.shouldShowFileExtensions = true
        return picker
    }
    func makeUIViewController(context: Context) -> NativeMusicPickerHost {
        let host = NativeMusicPickerHost()
        host.view.backgroundColor = .clear
        host.view.isUserInteractionEnabled = false
        return host
    }
    func updateUIViewController(_ host: NativeMusicPickerHost, context: Context) {
        context.coordinator.parent = self
        host.makePicker = {
            context.coordinator.prepareForNewPicker()
            return Self.openingController(delegate: context.coordinator)
        }
        host.wantsPicker = presented
        if presented {
            DispatchQueue.main.async { host.presentPickerIfNeeded() }
        } else if host.presentedViewController != nil {
            host.dismiss(animated: true) { host.resetIfIdle() }
        } else {
            host.resetIfIdle()
        }
    }
    @MainActor final class Coordinator: NSObject, UIDocumentPickerDelegate {
        var parent: NativeMusicDocumentPicker
        private var delivered = false
        init(_ parent: NativeMusicDocumentPicker) { self.parent = parent }
        func prepareForNewPicker() { delivered = false }
        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            guard !delivered else { return }; delivered = true
            NativeDiagnostics.shared.record(operation: .musicFileSelection, step: .acknowledged, target: .localPlayer)
            // Acquire the engine's security-scope leases before dismissing Files.
            parent.selected(urls)
            parent.presented = false
        }
        func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
            guard !delivered else { return }; delivered = true
            NativeDiagnostics.shared.record(operation: .musicFileSelection, step: .cancelled, target: .localPlayer)
            parent.presented = false
        }
    }
}

@MainActor struct NativeMusicImportStatus: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        if let progress = store.musicImport {
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Text("Файл \(progress.index) из \(progress.total)").font(.caption.weight(.medium))
                    Spacer()
                    if progress.phase == .uploading { Text(progress.fraction, format: .percent.precision(.fractionLength(0))).font(.caption.monospacedDigit()) }
                }
                Text(progress.fileName).font(.callout).lineLimit(2)
                if progress.phase == .uploading { ProgressView(value: progress.fraction) }
                else { ProgressView().controlSize(.small) }
                Text(phaseTitle(progress.phase)).font(.caption).foregroundStyle(.secondary)
                if progress.completed > 0 || progress.failed > 0 {
                    Text("Готово: \(progress.completed) · Не добавлено: \(progress.failed)").font(.caption).foregroundStyle(.secondary)
                }
            }.padding(.vertical, 5).accessibilityElement(children: .combine).accessibilityIdentifier("musicUploadProgress")
        }
    }
    private func phaseTitle(_ phase: NativeMusicImportPhase) -> String {
        switch phase {
        case .preparing: return "Подготовка файла из хранилища…"
        case .uploading: return "Загрузка на сервер…"
        case .finishing: return "Сервер проверяет и добавляет музыку…"
        }
    }
}

@MainActor struct NativeMusicImportView: View {
    @ObservedObject var store: NativeStore
    @Environment(\.dismiss) private var dismiss
    @State private var picker = false
    @State private var confirmCancel = false
    @State private var diagnostics = false
    var body: some View {
        NavigationStack {
            List {
                if store.error != nil || store.notice != nil { Section { NativeMessage(store: store) } }
                Section {
                    Label("Ваша музыка в XASS", systemImage: "music.note.list").font(.title3.weight(.semibold))
                    Text("Добавьте один трек, несколько файлов или ZIP. Исходные файлы останутся на месте.")
                        .font(.callout).foregroundStyle(.secondary)
                    Button {
                        NativeDiagnostics.shared.record(operation: .musicFileSelection, step: .requested, target: .localPlayer)
                        picker = true
                    } label: { Label("Выбрать в «Файлах»", systemImage: "folder.badge.plus").frame(maxWidth: .infinity, minHeight: 32) }
                        .buttonStyle(.borderedProminent).disabled(!store.authorized || store.isMusicImporting)
                        .accessibilityIdentifier("musicImportChooseFiles")
                } footer: {
                    Text("Выделите файлы и нажмите «Открыть» справа сверху. Если файл хранится в облаке, для подготовки потребуется интернет.")
                }
                if store.isMusicImporting {
                    Section("Импорт") {
                        NativeMusicImportStatus(store: store)
                        Button("Остановить импорт", role: .destructive) { confirmCancel = true }
                            .accessibilityIdentifier("musicImportCancel")
                        Text("Можно закрыть это окно. Не закрывайте XASS до окончания загрузки файлов; обработка уже отправленного ZIP продолжится на сервере.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                if !store.musicImportResults.isEmpty {
                    Section("Результат последнего импорта") {
                        ForEach(store.musicImportResults) { result in
                            HStack(alignment: .top, spacing: 12) {
                                Image(systemName: symbol(result.outcome)).foregroundStyle(color(result.outcome)).frame(width: 22)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(result.fileName).font(.callout.weight(.medium)).lineLimit(3)
                                    Text(result.message).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                                }
                            }.padding(.vertical, 4)
                        }
                        Button("Обновить библиотеку") { store.run { await store.refresh() } }
                    }.accessibilityIdentifier("musicImportResults")
                }
                Section {
                    LabeledContent("Аудиофайл", value: size(store.musicImportFileLimit))
                    LabeledContent("ZIP-архив", value: size(store.musicImportArchiveLimit))
                    Text("MP3, M4A, WAV, FLAC и OGG. В ZIP — до 200 аудиофайлов и 512 МиБ после распаковки. Повторные файлы не создают дубликатов.")
                        .font(.caption).foregroundStyle(.secondary)
                } header: {
                    Text("Лимиты этого сервера").accessibilityIdentifier("musicImportLimits")
                }
                Section {
                    Button { diagnostics = true } label: { Label("Отправить диагностику", systemImage: "square.and.arrow.up") }
                    Text("Отчёт покажет, дошёл ли выбор из «Файлов» до приложения и на каком этапе остановилась загрузка. Названия файлов и ключи в него не попадают.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.navigationTitle("Добавить музыку").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Готово") { dismiss() }.accessibilityIdentifier("musicImportDone") } }
                .background {
                    NativeMusicDocumentPicker(presented: $picker) { urls in store.importMusicFiles(urls) }
                }
                .sheet(isPresented: $diagnostics) {
                    NavigationStack { NativeDiagnosticLogView().toolbar { ToolbarItem(placement: .confirmationAction) { Button("Готово") { diagnostics = false } } } }
                }
                .confirmationDialog("Остановить оставшиеся файлы?", isPresented: $confirmCancel, titleVisibility: .visible) {
                    Button("Остановить импорт", role: .destructive) { store.cancelMusicImport() }
                    Button("Продолжить", role: .cancel) {}
                } message: { Text("Добавленные треки сохранятся. Если ZIP уже принят сервером, обработка может продолжиться: обновите библиотеку перед повторной загрузкой.") }
        }
    }
    private func size(_ bytes: Int) -> String { ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file) }
    private func symbol(_ outcome: NativeMusicImportOutcome) -> String {
        switch outcome {
        case .imported: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.circle"
        case .cancelled, .notAttempted: return "minus.circle"
        case .processing: return "clock"
        }
    }
    private func color(_ outcome: NativeMusicImportOutcome) -> Color {
        switch outcome {
        case .imported: return .green
        case .failed: return .orange
        case .cancelled, .notAttempted: return .secondary
        case .processing: return .blue
        }
    }
}
