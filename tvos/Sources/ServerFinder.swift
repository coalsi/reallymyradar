import Foundation
import Network

/// Finds ReallymyRadar servers on the local network via Bonjour (`_reallymyradar._tcp`).
/// Each server's TXT record carries ready-made URLs, so no address resolution is needed.
@MainActor
final class ServerFinder {
    private var browser: NWBrowser?

    /// Calls `onFound` with candidate base URLs (best first) whenever the set of servers changes.
    func start(onFound: @escaping @MainActor ([URL]) -> Void) {
        stop()
        let browser = NWBrowser(for: .bonjourWithTXTRecord(type: "_reallymyradar._tcp", domain: nil), using: .tcp)
        browser.browseResultsChangedHandler = { results, _ in
            var urls: [URL] = []
            for result in results {
                guard case let .bonjour(txt) = result.metadata else { continue }
                // The .local name survives the server's IP changing; the IP works where mDNS names don't.
                for key in ["host_url", "url"] {
                    if let s = txt[key], let url = URL(string: s), !urls.contains(url) { urls.append(url) }
                }
            }
            Task { @MainActor in onFound(urls) }
        }
        browser.start(queue: .main)
        self.browser = browser
    }

    func stop() {
        browser?.cancel()
        browser = nil
    }
}
