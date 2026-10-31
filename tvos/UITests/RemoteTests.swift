import XCTest

/// Drives the app with the Siri Remote the way a person would, against a running radar server.
final class RemoteTests: XCTestCase {
    private let app = XCUIApplication()
    private let remote = XCUIRemote.shared

    override func setUpWithError() throws {
        continueAfterFailure = false
        let server = ProcessInfo.processInfo.environment["RMR_SERVER"] ?? "http://127.0.0.1:8600"
        app.launchArguments += ["-server", server]
        app.launch()
        XCTAssertTrue(app.buttons["play"].waitForExistence(timeout: 30), "radar screen never appeared")
        XCTAssertTrue(waitForFocus("play"), "play button should start focused")
    }

    /// Every product and every map tool can be reached with the arrows, and back again.
    func testArrowsReachEveryControl() {
        let products = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'product.'"))
        XCTAssertGreaterThan(products.count, 3)

        for i in 0..<products.count {
            press(.right)
            XCTAssertEqual(focusedID, products.element(boundBy: i).identifier, "right #\(i + 1) along the product bar")
        }

        // Up from the bottom-right product reaches the map tools in the top-right corner.
        press(.up)
        XCTAssertTrue(focusedID.hasPrefix("tool."), "up from the products should reach the map tools, got \(focusedID)")
        for _ in 0..<6 { press(.left) }
        XCTAssertEqual(focusedID, "tool.move")
        for id in ["tool.zoomOut", "tool.zoomIn", "tool.home", "tool.look"] {
            press(.right)
            XCTAssertEqual(focusedID, id)
        }

        // And from the far left of the product bar too.
        press(.down)
        XCTAssertTrue(focusedID == "play" || focusedID.hasPrefix("product."), "down should return to the bar, got \(focusedID)")
        for _ in 0..<12 { press(.left) }
        XCTAssertEqual(focusedID, "play")
        press(.up)
        XCTAssertTrue(focusedID.hasPrefix("tool."), "up from play should reach the map tools, got \(focusedID)")
    }

    /// Zooming in deep keeps the radar connected and loads frames for the new view.
    func testZoomingInStaysConnected() {
        press(.up)
        for _ in 0..<6 { press(.left) }
        press(.right); press(.right)
        XCTAssertEqual(focusedID, "tool.zoomIn")
        for _ in 0..<4 {
            remote.press(.select)
            sleep(2)
        }
        // Long enough for the view's frames request plus a scheduled refresh.
        let source = app.staticTexts["source"]
        let deadline = Date.now.addingTimeInterval(70)
        while Date.now < deadline {
            XCTAssertFalse(source.label.localizedCaseInsensitiveContains("reach"), "lost the server after zooming: \(source.label)")
            sleep(2)
        }
        XCTAssertTrue(source.label.hasPrefix("Mosaic"), "unexpected source: \(source.label)")
    }

    /// Move map: arrows pan without losing focus, click zooms, Back returns to the controls.
    func testMoveMapMode() {
        press(.up)
        for _ in 0..<6 { press(.left) }
        XCTAssertEqual(focusedID, "tool.move")
        press(.select)
        XCTAssertTrue(waitForFocus("moveMap"), "move mode should take the arrows")
        for b in [XCUIRemote.Button.right, .right, .up, .left, .down] {
            press(b)
            XCTAssertEqual(focusedID, "moveMap", "arrows should pan, not move focus")
        }
        press(.select)
        XCTAssertEqual(focusedID, "moveMap")
        remote.press(.menu)
        XCTAssertTrue(waitForFocus("play"), "Back should return to the controls")
        sleep(1)  // tvOS ignores arrows while the controls fade back in
        press(.up)
        XCTAssertTrue(focusedID.hasPrefix("tool."), "controls should work after moving the map, got \(focusedID)")
        XCTAssertFalse(app.staticTexts["source"].label.localizedCaseInsensitiveContains("reach"))
    }

    /// Back hides the controls, any press brings them back with focus where you can use it.
    func testBackHidesAndAnyPressRestores() {
        remote.press(.menu)
        XCTAssertTrue(app.buttons["play"].waitForNonExistence(timeout: 5))
        remote.press(.down)
        XCTAssertTrue(app.buttons["play"].waitForExistence(timeout: 5))
        XCTAssertTrue(waitForFocus("play"))
        press(.right)
        XCTAssertTrue(focusedID.hasPrefix("product."), "arrows should work after the controls come back")
    }

    // MARK: - Helpers

    private var focusedID: String {
        let el = app.descendants(matching: .any).element(matching: NSPredicate(format: "hasFocus == true"))
        return el.exists ? el.identifier : "<nothing>"
    }

    private func press(_ button: XCUIRemote.Button) {
        remote.press(button)
        usleep(450_000)  // let the focus animation settle
    }

    private func waitForFocus(_ id: String, timeout: TimeInterval = 5) -> Bool {
        let deadline = Date.now.addingTimeInterval(timeout)
        while Date.now < deadline {
            if focusedID == id { return true }
            usleep(200_000)
        }
        return false
    }
}
