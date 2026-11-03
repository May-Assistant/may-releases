import SwiftUI

/// The tapper's host: XCUITest needs an app to run beside; this one shows a line and does nothing else.
@main
struct SpikeTapHost: App {
    var body: some Scene {
        WindowGroup { Text("May device tests: springboard tapper") }
    }
}
