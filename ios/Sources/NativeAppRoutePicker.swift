import SwiftUI

extension NativeStore {
    /// Preserve the actual source until the server confirms the handoff.
    /// pickRoute used to set selectedDevice first, bypassing transfer's source pause.
    func selectPlaybackRoute(device: String, output: String? = nil) async throws {
        guard !busy else { return }
        let chosenOutput = output ?? (device == selectedDevice ? outputID : "default")
        guard currentID != nil else {
            try await pickRoute(device: device, output: chosenOutput)
            return
        }
        showPlayer = false
        showRoutePicker = true
        do {
            // No optimistic selectedDevice/outputID assignment or stale position override.
            try await transfer(to: device, output: chosenOutput)
            showRoutePicker = false
        } catch {
            showRoutePicker = true
            throw error
        }
    }
}

@MainActor struct NativeAppRoutePicker: View {
    @ObservedObject var store: NativeStore
    @Environment(\.dismiss) private var dismiss
    @State private var targetDevice = "local"
    @State private var targetOutput = "default"
    @State private var loadingOutputs = false
    @State private var outputError: String?
    @State private var retry = 0

    private var selectedPC: NativeDevice? {
        store.devices.first { "agent:" + $0.name == targetDevice }
    }
    private var canConfirm: Bool {
        !store.busy && !loadingOutputs && outputError == nil &&
        (targetDevice == "local" || selectedPC?.online == true)
    }

    var body: some View {
        NavigationStack {
            List {
                if let status = store.transferStatus {
                    Section("Переключение") {
                        HStack { ProgressView(); Text(status) }
                        ProgressView(value: min(max(store.transferProgress, 0), 1))
                            .accessibilityIdentifier("nativeTransferProgress")
                        Button("Отмена") { store.cancelTransfer() }
                            .accessibilityIdentifier("nativeTransferCancel")
                    }
                } else {
                    Section("Устройство воспроизведения") {
                        Button {
                            targetDevice = "local"
                            loadingOutputs = false
                            targetOutput = "default"
                            outputError = nil
                        } label: {
                            HStack {
                                Label("Этот iPhone", systemImage: "iphone")
                                Spacer()
                                if targetDevice == "local" { Image(systemName: "checkmark") }
                            }
                        }.disabled(store.busy).accessibilityIdentifier("route-local")
                        ForEach(store.devices) { device in
                            Button {
                                guard targetDevice != "agent:" + device.name else { return }
                                loadingOutputs = true
                                targetOutput = "default"
                                outputError = nil
                                targetDevice = "agent:" + device.name
                            } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: "desktopcomputer")
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(device.name)
                                        Text(device.online ? "В сети" : "Не в сети")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if targetDevice == "agent:" + device.name { Image(systemName: "checkmark") }
                                }.padding(.vertical, 4)
                            }
                            .disabled(!device.online || store.busy)
                            .accessibilityIdentifier("route-device-\(device.id)")
                        }
                    }
                    if let device = selectedPC {
                        Section("Аудиовыход · \(device.name)") {
                            if loadingOutputs {
                                ProgressView("Получаю выходы Windows…")
                            } else if let outputError {
                                Text(outputError).foregroundStyle(.orange)
                                Button("Повторить") { retry += 1 }
                            } else {
                                outputRow("default", name: "Системный выход Windows")
                                ForEach(store.outputs.filter { $0.id != "default" }) { output in
                                    outputRow(output.id, name: output.name)
                                }
                            }
                        }
                    }
                    Section("AirPlay и наушники") {
                        if store.selectedDevice == "local" && !store.otherLocal {
                            HStack {
                                Label("Аудиовыход iPhone", systemImage: "airplayaudio")
                                Spacer()
                                RoutePicker().frame(width: 52, height: 52)
                                    .accessibilityIdentifier("nativeSystemAudioRoute")
                            }
                        } else {
                            Text("Сначала переключите воспроизведение на этот iPhone. После этого здесь можно выбрать наушники или AirPlay.")
                                .font(.callout).foregroundStyle(.secondary)
                        }
                    }
                }
                NativeMessage(store: store)
            }
            .navigationTitle("Где слушать")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Закрыть") { dismiss() }.disabled(store.busy)
                }
            }
            .safeAreaInset(edge: .bottom) {
                VStack(spacing: 10) {
                    Text("Перенесём воспроизведение с текущего места.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Button {
                        store.run {
                            try await store.selectPlaybackRoute(device: targetDevice, output: targetOutput)
                        }
                    } label: {
                        HStack {
                            Spacer()
                            if store.busy { ProgressView() }
                            Text(store.busy ? "Переключаю…" : "Переключить")
                            Spacer()
                        }.padding(.vertical, 10)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!canConfirm)
                    .accessibilityIdentifier("route-confirm")
                }.padding(16).background(.regularMaterial)
            }
            .onAppear {
                loadingOutputs = store.selectedDevice != "local"
                targetDevice = store.selectedDevice
                targetOutput = store.outputID
            }
            .task(id: "\(targetDevice):\(retry)") {
                guard let device = selectedPC else { loadingOutputs = false; return }
                loadingOutputs = true
                outputError = nil
                do {
                    try await store.loadOutputs(source: device.name)
                    try Task.checkCancellation()
                    if targetOutput != "default" && !store.outputs.contains(where: { $0.id == targetOutput }) {
                        targetOutput = "default"
                    }
                    loadingOutputs = false
                } catch is CancellationError {
                    // A different selection owns the loading state now.
                } catch {
                    guard !Task.isCancelled else { return }
                    outputError = error.localizedDescription
                    loadingOutputs = false
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .interactiveDismissDisabled(store.busy)
    }

    private func outputRow(_ id: String, name: String) -> some View {
        Button { targetOutput = id } label: {
            HStack {
                Label(name, systemImage: "speaker.wave.2")
                Spacer()
                if targetOutput == id { Image(systemName: "checkmark") }
            }
        }.disabled(store.busy)
    }
}
