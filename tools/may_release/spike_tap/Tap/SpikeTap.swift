import XCTest

/// May device tests' notification spike (may-releases device_ios.py): Maestro found a notification's text on the
/// simulator but its tap never reached May (run 41555424833), so this taps it through XCUITest on the springboard
/// itself. The notification is the one whose title is MAY_SPIKE_TITLE; MAY_SPIKE_WHERE is "banner" (as it shows) or
/// "centre" (Notification Centre, pulled down first). The test fails when no such notification is found.
final class SpikeTap: XCTestCase {
    func testTap() throws {
        let environment = ProcessInfo.processInfo.environment
        let title = try XCTUnwrap(environment["MAY_SPIKE_TITLE"], "no title was given")
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        if environment["MAY_SPIKE_WHERE"] == "centre" {
            let top = springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.01))
            top.press(forDuration: 0.1, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.7)))
        }
        let entry = springboard.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", title)).firstMatch
        XCTAssertTrue(entry.waitForExistence(timeout: 15), "no notification with the title")
        entry.tap()
        // A tap on a stacked group only expands it: tap the title once more while it still shows.
        if entry.waitForExistence(timeout: 2) && entry.isHittable {
            entry.tap()
        }
    }
}
