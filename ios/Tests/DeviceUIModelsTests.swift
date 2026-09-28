import XCTest
import SwiftUI
@testable import XASS

@MainActor private final class DeviceAdapterOwnerFixture: OwnerService {
    let origin = try! ServerOrigin("https://device-adapter-fixture.invalid")
    var requests: [(String, String)] = []
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method))
        return ["ok": true, "players": [], "session": [:]]
    }
}

final class DeviceUIModelsTests: XCTestCase {
    func testStatesExposeRetryBusyAndRussianCopy() {
        XCTAssertTrue(DeviceConnectionState.connecting(deviceName: "Студия", progress: 0.5, detail: nil).isBusy)
        XCTAssertTrue(DeviceConnectionState.switchFailed(deviceName: "Студия", message: "").canRetry)
        XCTAssertTrue(DeviceConnectionState.deviceOffline(deviceName: "Ноутбук").canRetry)
        XCTAssertFalse(DeviceConnectionState.playingRemotely(deviceName: "Студия").canRetry)
        XCTAssertFalse(DeviceConnectionState.noDevices.isBusy)
        XCTAssertEqual(DeviceConnectionState.deviceOffline(deviceName: "Ноутбук").title, "«Ноутбук» не в сети")
        XCTAssertNotNil(DeviceConnectionState.switchFailed(deviceName: "Студия", message: "").message, "An empty failure still explains itself")
        XCTAssertNil(DeviceConnectionState.idle.message)
    }

    func testSwitchResultHaptics() {
        let connecting = DeviceConnectionState.connecting(deviceName: "Студия", progress: nil, detail: nil)
        XCTAssertEqual(DeviceUIHaptics.result(from: connecting, to: .playingRemotely(deviceName: "Студия")), .success)
        XCTAssertEqual(DeviceUIHaptics.result(from: connecting, to: .switchFailed(deviceName: "Студия", message: "x")), .error)
        XCTAssertNil(DeviceUIHaptics.result(from: .idle, to: .playingRemotely(deviceName: "Студия")))
    }

    @MainActor func testFixtureOfflineSelectionFailsFastWithoutSwitching() {
        let model = FixtureDevicePickerModel(scenario: .normal, stepDelay: .milliseconds(1))
        model.select(FixtureDevicePickerModel.laptop)
        XCTAssertEqual(model.connectionState, .deviceOffline(deviceName: "Ноутбук"))
        XCTAssertEqual(model.activeDeviceID, PlaybackDevice.thisPhoneID)
        XCTAssertEqual(model.pendingDeviceID, FixtureDevicePickerModel.laptop.id)
    }

    @MainActor func testFixtureSwitchReachesRemoteAndFailureOffersRetry() async throws {
        let model = FixtureDevicePickerModel(scenario: .normal, stepDelay: .milliseconds(1))
        model.failNextSwitch = true
        model.select(FixtureDevicePickerModel.studio)
        XCTAssertTrue(model.connectionState.isBusy)
        for _ in 0..<200 where model.connectionState.isBusy { try await Task.sleep(for: .milliseconds(5)) }
        guard case .switchFailed = model.connectionState else { return XCTFail("Expected failure, got \(model.connectionState)") }
        model.retry()
        for _ in 0..<200 where model.activeDeviceID != FixtureDevicePickerModel.studio.id { try await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(model.activeDeviceID, FixtureDevicePickerModel.studio.id)
        XCTAssertEqual(model.connectionState, .playingRemotely(deviceName: "Студия"))
        XCTAssertNil(model.pendingDeviceID)
    }

    @MainActor func testFixtureEmptyAndRemoteControls() {
        XCTAssertEqual(FixtureDevicePickerModel(scenario: .empty).connectionState, .noDevices)
        let remote = FixtureRemotePlayback(ticking: false)
        remote.seek(to: 500); XCTAssertEqual(remote.position, remote.duration)
        remote.setVolume(-3); XCTAssertEqual(remote.volume, 0)
        let wasPlaying = remote.isPlaying; remote.playPause(); XCTAssertNotEqual(remote.isPlaying, wasPlaying)
        let first = remote.track; remote.next(); XCTAssertNotEqual(remote.track, first)
        XCTAssertFalse(FixtureRemotePlayback(offline: true, ticking: false).controlsEnabled)
    }

    @MainActor func testAdapterMapsStoreStateReadOnly() {
        let api = DeviceAdapterOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        let adapter = NativeStoreDeviceAdapter(store: store)
        XCTAssertEqual(adapter.connectionState, .noDevices)
        XCTAssertEqual(adapter.devices.map(\.id), ["local"])
        store.devices = [
            ["id": 1, "source_type": "PC_AGENT", "source_name": "Студия", "is_online": true],
            ["id": 2, "source_type": "PC_AGENT", "source_name": "Ноутбук", "is_online": false]
        ].compactMap(NativeDevice.init)
        XCTAssertEqual(adapter.connectionState, .idle)
        XCTAssertEqual(adapter.devices.map(\.accessibilityKey), ["local", "device-1", "device-2"])
        store.selectedDevice = "agent:Студия"
        XCTAssertEqual(adapter.connectionState, .playingRemotely(deviceName: "Студия"))
        XCTAssertEqual(adapter.activeDeviceID, "agent:Студия")
        store.selectedDevice = "local"
        adapter.select(adapter.devices[2])
        XCTAssertEqual(adapter.connectionState, .deviceOffline(deviceName: "Ноутбук"))
        XCTAssertTrue(api.requests.isEmpty, "Selecting an offline PC must not send anything")
    }
}
