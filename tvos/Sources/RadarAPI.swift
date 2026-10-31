import Foundation

/// Thin client for a ReallymyRadar server on the local network.
struct RadarAPI {
    let base: URL

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    /// API calls get their own connections. Sharing them with the map's tile downloads, a call made
    /// right after a zoom waited behind hundreds of tiles and timed out ("can't reach the server").
    private static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.httpMaximumConnectionsPerHost = 2
        c.timeoutIntervalForRequest = 20
        return URLSession(configuration: c)
    }()

    func get<T: Decodable>(_ path: String, _ query: [String: String] = [:], as type: T.Type = T.self) async throws -> T {
        var comps = URLComponents(url: base.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            comps.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        let request = URLRequest(url: comps.url!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        let (data, response) = try await Self.session.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        return try Self.decoder.decode(T.self, from: data)
    }

    /// True when a ReallymyRadar server answers at `base`.
    func isAlive() async -> Bool {
        struct Status: Decodable { var workers: Int }
        return (try? await get("/api/status", as: Status.self)) != nil
    }

    func tileTemplate(source: String, product: String, frame: Frame) -> String {
        // MapKit fills {z}/{x}/{y}; tiles are 512 px renders of standard XYZ tiles.
        "\(base.absoluteString)/tiles/\(source)/\(product)/0/\(frame.id)/{z}/{x}/{y}.png?r=\(frame.rev)"
    }
}
