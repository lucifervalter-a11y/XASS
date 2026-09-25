import XCTest
@testable import XASS

@MainActor private final class WorkspaceOwnerFixture: OwnerService {
    let origin = try! ServerOrigin("https://workspace-fixture.invalid")
    var site: [String: Any] = ["ok": true, "can_edit": true, "projects": []]
    var writes: [(path: String, method: String, body: [String: Any])] = []
    var rejectWrites = false

    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        if path == "/api/mini/site", method == "GET" { return site }
        guard let body = body, method == "POST" else { throw OwnerAPIError.invalidResponse }
        writes.append((path, method, body))
        if rejectWrites { throw OwnerAPIError(status: 503, message: "Сервер недоступен") }
        switch path {
        case "/api/mini/site/profile":
            site["profile"] = body
            return ["ok": true, "profile": body]
        case "/api/mini/site/projects":
            var saved = body
            saved["years"] = ["from": body["year_from"]!, "to": body["year_to"]!]
            saved["cover"] = ["type": body["cover_type"]!, "src": body["cover_src"]!]
            site["projects"] = [saved]
            return ["ok": true, "projects": [saved]]
        default: throw OwnerAPIError.invalidResponse
        }
    }
}

final class NativeWorkspaceStoreTests: XCTestCase {
    @MainActor func testProfileEditPreservesExistingFieldsAndFailedSaveKeepsSavedState() async throws {
        let api = WorkspaceOwnerFixture()
        api.site["profile"] = ["name": "Original", "title": "Developer", "bio": "Profile text",
                               "username": "owner", "telegram_url": "https://t.me/owner",
                               "avatar_url": "/data/avatars/owner.jpg", "quote": "Keep this quote",
                               "stack": ["Swift", "Python"], "links": [["label": "Project", "url": "https://example.com/project"]]]
        let workspace = NativeWorkspaceStore(api: api)
        await workspace.load("site")
        var draft = workspace.profile
        draft.name = "Updated"
        let saved = await workspace.saveProfile(draft)
        XCTAssertTrue(saved)
        let payload = try XCTUnwrap(api.writes.last?.body)
        XCTAssertEqual(payload["name"] as? String, "Updated")
        XCTAssertEqual(payload["avatar_url"] as? String, "/data/avatars/owner.jpg")
        XCTAssertEqual(payload["telegram_url"] as? String, "https://t.me/owner")
        XCTAssertEqual(payload["quote"] as? String, "Keep this quote")
        XCTAssertEqual(payload["stack"] as? [String], ["Swift", "Python"])
        XCTAssertEqual(payload["links"] as? [[String: String]], [["label": "Project", "url": "https://example.com/project"]])

        api.rejectWrites = true
        draft.name = "Unsaved draft"
        let rejected = await workspace.saveProfile(draft)
        XCTAssertFalse(rejected)
        XCTAssertEqual(workspace.profile.name, "Updated", "An unsuccessful save must not replace the last server-confirmed profile")
        XCTAssertFalse(workspace.saving)
        XCTAssertNil(workspace.notice)
        XCTAssertNotNil(workspace.error)
    }

    @MainActor func testProjectTitleEditPreservesCoverYearRangeTagsAndIdentity() async throws {
        let api = WorkspaceOwnerFixture()
        api.site["projects"] = [["id": "robot-controller", "title": "Controller", "subtitle": "Mechatronics",
                                  "description": "Project description", "url": "https://example.com/controller", "status": "done",
                                  "years": ["from": 2024, "to": 2026], "tags": ["PLC", "Robotics"], "featured": true,
                                  "cover": ["type": "image", "src": "/data/projects/controller.png"]]]
        let workspace = NativeWorkspaceStore(api: api)
        await workspace.load("site")
        var draft = try XCTUnwrap(workspace.projects.first)
        draft.title = "New title"
        let saved = await workspace.saveProject(draft)
        XCTAssertTrue(saved)
        let payload = try XCTUnwrap(api.writes.last?.body)
        XCTAssertEqual(payload["id"] as? String, "robot-controller")
        XCTAssertEqual(payload["year_from"] as? Int, 2024)
        XCTAssertEqual(payload["year_to"] as? Int, 2026)
        XCTAssertEqual(payload["cover_type"] as? String, "image")
        XCTAssertEqual(payload["cover_src"] as? String, "/data/projects/controller.png")
        XCTAssertEqual(payload["tags"] as? [String], ["PLC", "Robotics"])
        XCTAssertEqual(payload["featured"] as? Bool, true)
        XCTAssertEqual(workspace.projects.first?.title, "New title")
        XCTAssertEqual(workspace.projects.first?.coverSource, "/data/projects/controller.png")
    }
}
