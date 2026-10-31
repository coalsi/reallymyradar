import Foundation
import MapKit
import Observation
import SwiftUI

/// Everything the TV app shows: which server, which product, the animation frames and where the map is.
@MainActor
@Observable
final class RadarModel {
    enum Phase: Equatable {
        case searching
        case connecting(String)
        case ready
        case failed(String)
    }

    private(set) var phase: Phase = .searching
    private(set) var products: [String] = []
    private(set) var product: String
    private(set) var layers: [RadarLayer] = []
    private(set) var currentLayerID: String?
    private(set) var frameTimes: [Date] = []
    private(set) var frameIndex = 0
    private(set) var liveFrame = false
    private(set) var playing: Bool
    private(set) var sourceLabel = ""
    private(set) var legend: Legend?
    private(set) var opacity = 0.85
    private(set) var look: MapLook
    private(set) var home: HomePoint?
    private(set) var homeLabel: String?
    private(set) var camera: CameraRequest?
    private(set) var serverName = ""

    @ObservationIgnored private var api: RadarAPI?
    @ObservationIgnored private var config: ServerConfig?
    @ObservationIgnored private var frames: [Frame] = []
    @ObservationIgnored private var source = "MOSAIC"
    @ObservationIgnored private var bounds: (south: Double, west: Double, north: Double, east: Double)?
    @ObservationIgnored private var lastFetchBounds: (south: Double, west: Double, north: Double, east: Double)?
    @ObservationIgnored private let finder = ServerFinder()
    @ObservationIgnored private var playTask: Task<Void, Never>?
    @ObservationIgnored private var refreshTask: Task<Void, Never>?
    @ObservationIgnored private var boundsTask: Task<Void, Never>?
    @ObservationIgnored private var cameraSeq = 0
    @ObservationIgnored private let defaults = UserDefaults.standard

    private static let maxFrames = 10

    init() {
        product = defaults.string(forKey: "product") ?? "CREF"
        look = MapLook(rawValue: defaults.string(forKey: "look") ?? "") ?? .dark
        playing = defaults.object(forKey: "playing") as? Bool ?? true
    }

    // MARK: - Connecting

    func start() async {
        if let saved = defaults.string(forKey: "server").flatMap(URL.init(string:)), await connect(to: saved) {
            return
        }
        discover()
    }

    func discover() {
        phase = .searching
        finder.start { [weak self] urls in
            guard let self else { return }
            Task { await self.tryCandidates(urls) }
        }
    }

    private func tryCandidates(_ urls: [URL]) async {
        guard phase == .searching else { return }
        for url in urls where await connect(to: url) {
            finder.stop()
            return
        }
    }

    /// Manual entry: "192.168.1.20", "mypc.local:8600" or a full URL.
    func useAddress(_ text: String) async {
        var s = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return }
        if !s.contains("://") { s = "http://" + s }
        guard var comps = URLComponents(string: s) else { return }
        if comps.port == nil, comps.scheme == "http" { comps.port = 8600 }
        guard let url = comps.url else { return }
        if await !connect(to: url) {
            phase = .failed("Couldn't reach ReallymyRadar at \(url.host() ?? s). Is it running?")
        }
    }

    private func connect(to url: URL) async -> Bool {
        phase = .connecting(url.host() ?? url.absoluteString)
        let api = RadarAPI(base: url)
        guard await api.isAlive(), let cfg = try? await api.get("/api/config", as: ServerConfig.self) else {
            phase = .searching
            return false
        }
        self.api = api
        config = cfg
        serverName = url.host() ?? ""
        defaults.set(url.absoluteString, forKey: "server")
        products = cfg.products
        if !products.contains(product) { product = products.first ?? "CREF" }
        if let lat = cfg.home.lat, let lon = cfg.home.lon {
            home = HomePoint(latitude: lat, longitude: lon)
            homeLabel = cfg.home.label
            if bounds == nil {
                if let r = RadarMapView.launchRegion() {
                    let (la, lo, h, w) = (r.center.latitude, r.center.longitude, r.span.latitudeDelta / 2, r.span.longitudeDelta / 2)
                    bounds = (la - h, lo - w, la + h, lo + w)
                } else {
                    bounds = (lat - 1.6, lon - 2.6, lat + 1.6, lon + 2.6)
                    moveCamera(.home)
                }
            }
        }
        updateLegend()
        phase = .ready
        await refreshFrames()
        startLoops()
        return true
    }

    func forgetServer() {
        defaults.removeObject(forKey: "server")
        playTask?.cancel()
        refreshTask?.cancel()
        api = nil
        layers = []
        discover()
    }

    // MARK: - Frames

    private func startLoops() {
        refreshTask?.cancel()
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(45))
                await self?.refreshFrames()
            }
        }
        if playing { startPlaying() }
    }

    func refreshFrames() async {
        guard let api else { return }
        let b = bounds ?? (24, -125, 50, -66)
        let product = self.product
        do {
            let newFrames: [Frame]
            let newSource: String
            if Product.mosaicable.contains(product) {
                let r = try await api.get("/api/mosaic", [
                    "south": "\(b.south)", "west": "\(b.west)", "north": "\(b.north)", "east": "\(b.east)",
                    "product": product,
                ], as: MosaicResponse.self)
                newFrames = r.frames
                newSource = "MOSAIC"
                let ready = r.members.filter { $0.frames > 0 }.count
                sourceLabel = r.loading ? "Mosaic · loading \(ready) of \(r.members.count) radars"
                    : "Mosaic · \(r.members.count) radars"
            } else {
                // Velocity and friends are relative to one radar: use the one nearest the map's center.
                let lat = (b.south + b.north) / 2, lon = (b.west + b.east) / 2
                let near = try await api.get("/api/sites/near", ["lat": "\(lat)", "lon": "\(lon)", "n": "1",
                                                                  "wsr_only": "true"], as: [NearSite].self)
                guard let site = near.first?.id else { return }
                let r = try await api.get("/api/site/\(site)", ["product": product], as: SiteResponse.self)
                newFrames = r.frames
                newSource = site
                sourceLabel = "\(site) radar"
            }
            guard product == self.product else { return }  // switched while loading
            lastFetchBounds = b
            apply(frames: Array(newFrames.suffix(Self.maxFrames)), source: newSource)
        } catch {
            sourceLabel = "Can't reach the radar server"
        }
    }

    private func apply(frames newFrames: [Frame], source newSource: String) {
        let followLatest = playing || frameIndex >= frames.count - 1
        let previousID = frames.indices.contains(frameIndex) ? frames[frameIndex].id : nil
        frames = newFrames
        source = newSource
        guard let api else { return }
        layers = newFrames.map {
            RadarLayer(id: "\(newSource)/\(product)/\($0.rev)", urlTemplate: api.tileTemplate(source: newSource, product: product, frame: $0))
        }
        frameTimes = newFrames.map { Self.parseTime($0.time) ?? .now }
        if followLatest || previousID == nil {
            frameIndex = max(0, newFrames.count - 1)
        } else {
            frameIndex = newFrames.firstIndex { $0.id == previousID } ?? max(0, newFrames.count - 1)
        }
        showFrame(frameIndex)
    }

    private func showFrame(_ i: Int) {
        guard !layers.isEmpty else {
            currentLayerID = nil
            return
        }
        frameIndex = (i + layers.count) % layers.count
        currentLayerID = layers[frameIndex].id
        liveFrame = !frames[frameIndex].complete
    }

    // MARK: - Playback

    func togglePlay() {
        playing.toggle()
        defaults.set(playing, forKey: "playing")
        if playing { startPlaying() } else {
            playTask?.cancel()
            showFrame(layers.count - 1)  // pausing lands on the latest
        }
    }

    private func startPlaying() {
        playTask?.cancel()
        playTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let atEnd = frameIndex >= layers.count - 1
                try? await Task.sleep(for: atEnd ? .milliseconds(2200) : .milliseconds(650))
                if Task.isCancelled { return }
                showFrame(frameIndex + 1)
            }
        }
    }

    func step(_ delta: Int) {
        if playing { togglePlay() }
        showFrame(frameIndex + delta)
    }

    // MARK: - Product, look, camera

    func select(product code: String) {
        guard code != product else { return }
        product = code
        defaults.set(code, forKey: "product")
        layers = []
        currentLayerID = nil
        updateLegend()
        Task { await refreshFrames() }
    }

    func cycleLook() {
        look = look.next
        defaults.set(look.rawValue, forKey: "look")
    }

    func pan(_ direction: MoveCommandDirection) {
        let step = 0.33
        switch direction {
        case .left: moveCamera(.pan(dx: -step, dy: 0))
        case .right: moveCamera(.pan(dx: step, dy: 0))
        case .up: moveCamera(.pan(dx: 0, dy: step))
        case .down: moveCamera(.pan(dx: 0, dy: -step))
        @unknown default: break
        }
    }

    func zoom(in zoomIn: Bool) { moveCamera(.zoom(factor: zoomIn ? 0.5 : 2)) }
    func goHome() { moveCamera(.home) }

    private func moveCamera(_ kind: CameraRequest.Kind) {
        cameraSeq += 1
        camera = CameraRequest(id: cameraSeq, kind: kind)
    }

    /// The map reports where it is; the mosaic follows the view (which radars, nearest radar).
    func regionChanged(south: Double, west: Double, north: Double, east: Double) {
        bounds = (south, west, north, east)
        guard let last = lastFetchBounds else { return }
        let span = max(last.north - last.south, 0.01)
        let moved = abs((south + north) / 2 - (last.south + last.north) / 2) / span
            + abs((west + east) / 2 - (last.west + last.east) / 2) / max(last.east - last.west, 0.01)
        let zoomed = abs(log2((north - south) / span))
        guard moved > 0.3 || zoomed > 0.9 else { return }
        boundsTask?.cancel()
        boundsTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(800))
            if Task.isCancelled { return }
            await self?.refreshFrames()
        }
    }

    // MARK: - Legend

    private func updateLegend() {
        guard let cfg = config, let style = cfg.settings.render.styles[product],
              let palette = cfg.palettes[style.palette] else {
            legend = nil
            return
        }
        opacity = style.opacity
        let span = style.vmax - style.vmin
        let marks = palette.stops.map {
            Legend.Mark(location: min(1, max(0, ($0.value - style.vmin) / span)),
                        color: Color(hex: $0.hex, opacity: max($0.alpha, 0.35)))
        }
        let isSpeed = product == "VEL" || product == "SW"
        let units = isSpeed ? cfg.settings.render.velocityUnits : palette.units
        let factor: Double = switch (isSpeed, units) {
        case (true, "kt"): 1.94384   // server stores m/s
        case (true, "mph"): 2.23694
        default: 1
        }
        let ticks = (0...4).map { i -> String in
            let v: Double = (style.vmin + span * Double(i) / 4) * factor
            return span > 5 ? String(Int(v.rounded())) : String(format: "%.2f", v)
        }
        legend = Legend(title: Product.name(product), units: units, marks: marks, ticks: ticks)
    }

    // MARK: - Time

    var timeText: String {
        guard frameTimes.indices.contains(frameIndex) else { return "--:--" }
        return frameTimes[frameIndex].formatted(date: .omitted, time: .shortened)
    }

    var ageText: String {
        guard frameTimes.indices.contains(frameIndex) else { return "" }
        if liveFrame { return "LIVE" }
        let minutes = Int(Date.now.timeIntervalSince(frameTimes[frameIndex]) / 60)
        return minutes <= 1 ? "now" : "\(minutes) min ago"
    }

    private static func parseTime(_ s: String) -> Date? {
        // Server times may carry microseconds, which ISO8601DateFormatter can't parse.
        let trimmed = s.replacingOccurrences(of: #"\.\d+"#, with: "", options: .regularExpression)
        return ISO8601DateFormatter().date(from: trimmed)
    }
}
