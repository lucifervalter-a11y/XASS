import SwiftUI

/// NativeStore-specific pieces hosted inside the store-agnostic Now Playing view.
@MainActor struct NativePlayerStatus: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        VStack(spacing: 10) {
            if store.busy || store.playbackState == "loading" || store.transferStatus != nil {
                // While Now Playing is open, transfer progress lives here, never as a banner on top.
                HStack(spacing: 8) {
                    ProgressView().tint(.white)
                    Text(store.transferStatus ?? (store.busy ? "Выполняю действие…" : "Загрузка трека…")).font(.caption)
                    if store.transferStatus != nil {
                        Button("Отмена") { store.cancelTransfer() }
                            .font(.caption.weight(.semibold)).accessibilityIdentifier("nativeTransferCancel")
                    }
                }.accessibilityIdentifier("nativePlayerLoading")
            }
            if store.otherLocal {
                Text("Команды выполняются после ответа другого устройства. Для переноса выберите «Этот iPhone» ниже.")
                    .font(.caption).foregroundStyle(.white.opacity(0.6)).multilineTextAlignment(.center)
            }
            if store.shareSite {
                Label("Сейчас на вашем сайте", systemImage: "dot.radiowaves.left.and.right").font(.caption2).foregroundStyle(.white.opacity(0.55))
            }
            NativeMessage(store: store)
        }
    }
}

@MainActor struct NativeFavoriteButton: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        if let track = store.currentTrack {
            Button { PlayerHaptics.tap(); store.run { try await store.favorite(track) } } label: {
                Image(systemName: track.favorite ? "star.fill" : "star").font(.title3).frame(width: 44, height: 44)
            }
            .disabled(store.busy)
            .accessibilityLabel(track.favorite ? "Убрать из избранного" : "В избранное")
        }
    }
}
