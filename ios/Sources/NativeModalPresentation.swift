import Foundation

/// The app has one presentation host. A replacement waits for dismissal to finish.
enum NativeAppModal: String, Identifiable {
    case enrollment, routes, player, devicePreview, storagePreview
    var id: String { rawValue }
}

struct NativeModalPresentation {
    private(set) var current: NativeAppModal?
    private(set) var pending: NativeAppModal?
    private(set) var isDismissing = false

    mutating func request(_ destination: NativeAppModal?) {
        if isDismissing {
            pending = destination
        } else if current != destination {
            if current == nil {
                current = destination
            } else {
                pending = destination
                isDismissing = true
                current = nil
            }
        }
    }

    mutating func systemDismissed() { current = nil }

    /// True means a user dismissal, so the view should clear its old requests.
    @discardableResult mutating func didDismiss() -> Bool {
        let userDismissal = !isDismissing
        current = pending
        pending = nil
        isDismissing = false
        return userDismissal
    }
}
