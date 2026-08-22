// Local server discovery over mDNS / Bonjour (SPEC.md §3).
//
// Browses the local subnet for RoomSplat servers publishing `_roomsplat._tcp` or `_http._tcp`.
// When discovered, offers the server endpoint (hostname:port) directly to the UI.

import Foundation
import Network

struct DiscoveredServer: Identifiable, Hashable {
    let id: String
    let name: String
    let host: String
    let port: Int

    var endpointString: String { "\(host):\(port)" }
}

final class ServerDiscovery: ObservableObject {
    @Published private(set) var discoveredServers: [DiscoveredServer] = []
    @Published private(set) var isSearching = false

    private var browser: NWBrowser?
    private let queue = DispatchQueue(label: "roomsplat.discovery")

    func start() {
        guard !isSearching else { return }
        discoveredServers.removeAll()
        isSearching = true

        let parameters = NWParameters()
        parameters.includePeerToPeer = true

        let descriptor = NWBrowser.Descriptor.bonjour(type: "_roomsplat._tcp", domain: "local.")
        let browser = NWBrowser(for: descriptor, using: parameters)

        browser.browseResultsChangedHandler = { [weak self] results, changes in
            self?.handleResults(results)
        }

        browser.stateUpdateHandler = { [weak self] state in
            DispatchQueue.main.async {
                switch state {
                case .ready:
                    self?.isSearching = true
                case .failed(let error):
                    print("[discovery] browser failed:", error)
                    self?.isSearching = false
                case .cancelled:
                    self?.isSearching = false
                default:
                    break
                }
            }
        }

        browser.start(queue: queue)
        self.browser = browser
    }

    func stop() {
        browser?.cancel()
        browser = nil
        DispatchQueue.main.async {
            self.isSearching = false
        }
    }

    private func handleResults(_ results: Set<NWBrowser.Result>) {
        var servers: [DiscoveredServer] = []
        for result in results {
            if case let .service(name, type, domain, _) = result.endpoint {
                let id = "\(name).\(type).\(domain)"
                // Derive host & default port (8000 for roomsplat server)
                let host = "\(name).local"
                servers.append(DiscoveredServer(id: id, name: name, host: host, port: 8000))
            }
        }

        DispatchQueue.main.async {
            self.discoveredServers = servers
        }
    }
}
