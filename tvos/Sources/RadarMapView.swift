import MapKit
import SwiftUI

/// Apple Maps with the radar mosaic on top: one `MKTileOverlay` per animation frame, only the
/// current one drawn. Hidden frames stay on the map so their tiles are already loaded when
/// the loop reaches them.
struct RadarMapView: UIViewRepresentable {
    let layers: [RadarLayer]
    let currentLayerID: String?
    let opacity: Double
    let look: MapLook
    let camera: CameraRequest?
    let home: HomePoint?
    let onRegionChange: (_ south: Double, _ west: Double, _ north: Double, _ east: Double) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MKMapView {
        let map = MKMapView()
        // MapKit on tvOS makes the map focusable: the first arrow press moved focus onto it, then every
        // swipe panned it and focus never came back to the controls. It only moves on command here.
        map.isUserInteractionEnabled = false
        map.delegate = context.coordinator
        map.overrideUserInterfaceStyle = .dark
        map.pointOfInterestFilter = .excludingAll
        if let region = Self.launchRegion() {
            map.setRegion(region, animated: false)
            context.coordinator.lastCameraID = Int.max  // keep this view; ignore the initial "go home"
        } else if let home {
            map.setRegion(MKCoordinateRegion(center: home.coordinate,
                                             span: MKCoordinateSpan(latitudeDelta: 3.2, longitudeDelta: 5.2)),
                          animated: false)
        }
        return map
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        let c = context.coordinator
        c.onRegionChange = onRegionChange
        c.applyLook(look, to: map)
        c.syncLayers(layers, current: currentLayerID, opacity: opacity, on: map)
        c.syncHome(home, on: map)
        if let camera, camera.id != c.lastCameraID {
            c.lastCameraID = camera.id
            c.apply(camera.kind, home: home, to: map)
        }
    }

    /// Optional starting view, e.g. for a TV that always watches one area:
    /// launch arguments / defaults `startLat`, `startLon`, `startSpan` (degrees of latitude).
    static func launchRegion() -> MKCoordinateRegion? {
        guard let lat = launchValue("startLat"), let lon = launchValue("startLon") else { return nil }
        let span = launchValue("startSpan") ?? 3.2
        return MKCoordinateRegion(center: CLLocationCoordinate2D(latitude: lat, longitude: lon),
                                  span: MKCoordinateSpan(latitudeDelta: span, longitudeDelta: span * 1.6))
    }

    /// `-name value` from the launch arguments, else the saved default. Read directly because the
    /// automatic argument parsing mistakes a negative number (every US longitude) for a new option.
    private static func launchValue(_ name: String) -> Double? {
        let args = ProcessInfo.processInfo.arguments
        if let i = args.firstIndex(of: "-" + name), i + 1 < args.count, let v = Double(args[i + 1]) { return v }
        return UserDefaults.standard.object(forKey: name) != nil ? UserDefaults.standard.double(forKey: name) : nil
    }

    final class Coordinator: NSObject, MKMapViewDelegate {
        var onRegionChange: ((Double, Double, Double, Double) -> Void)?
        var lastCameraID = 0
        private var overlays: [String: QueuedTileOverlay] = [:]
        private var renderers: [String: FrameRenderer] = [:]
        private var look: MapLook?
        private var homeAnnotation: MKPointAnnotation?
        private var visibleID: String?
        private var opacity = 0.85

        func applyLook(_ newLook: MapLook, to map: MKMapView) {
            guard newLook != look else { return }
            look = newLook
            switch newLook {
            case .dark:
                map.preferredConfiguration = MKStandardMapConfiguration(elevationStyle: .flat, emphasisStyle: .muted)
            case .hybrid:
                map.preferredConfiguration = MKHybridMapConfiguration(elevationStyle: .flat)
            case .satellite:
                map.preferredConfiguration = MKImageryMapConfiguration(elevationStyle: .flat)
            }
        }

        func syncLayers(_ layers: [RadarLayer], current: String?, opacity: Double, on map: MKMapView) {
            self.opacity = opacity
            let wanted = Set(layers.map(\.id))
            for layer in layers where overlays[layer.id] == nil {
                let overlay = QueuedTileOverlay(urlTemplate: layer.urlTemplate)
                overlay.tileSize = CGSize(width: 512, height: 512)
                overlay.canReplaceMapContent = false
                overlay.minimumZ = 3
                overlay.maximumZ = 14
                overlays[layer.id] = overlay
                // Above roads, below place names, so towns stay readable over the storms.
                map.addOverlay(overlay, level: .aboveRoads)
            }
            for (id, overlay) in overlays where !wanted.contains(id) {
                map.removeOverlay(overlay)
                TileLoader.shared.cancel(for: overlay)
                overlays[id] = nil
                renderers[id] = nil
            }
            visibleID = current
            for (id, overlay) in overlays { overlay.onScreen = id == current }
            for (id, renderer) in renderers where renderer.shown != (id == current) {
                renderer.shown = id == current
                renderer.setNeedsDisplay()
            }
        }

        func syncHome(_ home: HomePoint?, on map: MKMapView) {
            guard let home else { return }
            if let a = homeAnnotation, a.coordinate.latitude == home.latitude, a.coordinate.longitude == home.longitude {
                return
            }
            if let a = homeAnnotation { map.removeAnnotation(a) }
            let a = MKPointAnnotation()
            a.coordinate = home.coordinate
            a.title = "Home"
            map.addAnnotation(a)
            homeAnnotation = a
        }

        func apply(_ kind: CameraRequest.Kind, home: HomePoint?, to map: MKMapView) {
            var region = map.region
            switch kind {
            case let .pan(dx, dy):
                region.center.latitude = min(72, max(10, region.center.latitude + dy * region.span.latitudeDelta))
                region.center.longitude += dx * region.span.longitudeDelta
            case let .zoom(factor):
                region.span.latitudeDelta = min(60, max(0.04, region.span.latitudeDelta * factor))
                region.span.longitudeDelta = min(100, max(0.06, region.span.longitudeDelta * factor))
            case .home:
                guard let home else { return }
                region = MKCoordinateRegion(center: home.coordinate,
                                            span: MKCoordinateSpan(latitudeDelta: 3.2, longitudeDelta: 5.2))
            }
            map.setRegion(region, animated: true)
        }

        // MARK: MKMapViewDelegate

        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            guard let tile = overlay as? MKTileOverlay,
                  let id = overlays.first(where: { $0.value === tile })?.key else {
                return MKOverlayRenderer(overlay: overlay)
            }
            let r = FrameRenderer(tileOverlay: tile)
            r.alpha = CGFloat(opacity)
            r.shown = id == visibleID
            renderers[id] = r
            return r
        }

        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            guard annotation === homeAnnotation else { return nil }
            let v = MKMarkerAnnotationView(annotation: annotation, reuseIdentifier: "home")
            v.glyphImage = UIImage(systemName: "house.fill")
            v.markerTintColor = UIColor(red: 0.30, green: 0.71, blue: 1, alpha: 1)
            v.displayPriority = .required
            return v
        }

        func mapView(_ mapView: MKMapView, regionDidChangeAnimated animated: Bool) {
            let r = mapView.region
            onRegionChange?(r.center.latitude - r.span.latitudeDelta / 2, r.center.longitude - r.span.longitudeDelta / 2,
                            r.center.latitude + r.span.latitudeDelta / 2, r.center.longitude + r.span.longitudeDelta / 2)
        }
    }
}

private extension HomePoint {
    var coordinate: CLLocationCoordinate2D { CLLocationCoordinate2D(latitude: latitude, longitude: longitude) }
}

/// Tile renderer for one animation frame. MapKit fixes a renderer's opacity when it's created
/// and ignores later `alpha` changes, so frames are switched by skipping the draw instead.
/// Tiles keep loading while hidden, so the frame appears instantly when its turn comes.
nonisolated final class FrameRenderer: MKTileOverlayRenderer {
    private let lock = NSLock()
    private var _shown = false

    /// Set on the main thread; read by MapKit's drawing threads.
    var shown: Bool {
        get { lock.withLock { _shown } }
        set { lock.withLock { _shown = newValue } }
    }

    override func draw(_ mapRect: MKMapRect, zoomScale: MKZoomScale, in context: CGContext) {
        guard shown else { return }
        super.draw(mapRect, zoomScale: zoomScale, in: context)
    }
}

/// One animation frame's tiles, fetched through `TileLoader` so the frame on screen comes first.
nonisolated final class QueuedTileOverlay: MKTileOverlay, @unchecked Sendable {
    private let lock = NSLock()
    private var _onScreen = false

    /// Set on the main thread; read by the loader's threads.
    var onScreen: Bool {
        get { lock.withLock { _onScreen } }
        set { lock.withLock { _onScreen = newValue } }
    }

    override func loadTile(at path: MKTileOverlayPath, result: @escaping @Sendable (Data?, (any Error)?) -> Void) {
        TileLoader.shared.load(url(forTilePath: path), zoom: path.z, for: self, result: result)
    }
}

/// Downloads radar tiles a few at a time, most useful first. MapKit asks for every frame's tiles
/// at once (hundreds after a zoom) and can't cancel them, so a plain URLSession queue served them
/// in order: the picture on screen filled in last, and slow to appear. Here the current zoom level
/// beats leftovers from the last one, the frame on screen beats the others, and newer beats older.
/// Downloads are kept in memory: the animation cycles every frame, so they're asked for again and again.
nonisolated final class TileLoader: @unchecked Sendable {
    static let shared = TileLoader()

    private struct Job: Sendable {
        let url: URL
        let zoom: Int
        let order: Int
        let overlay: QueuedTileOverlay
        let result: @Sendable (Data?, (any Error)?) -> Void
    }

    private let lock = NSLock()
    private var waiting: [Job] = []
    private var running = 0
    private var counter = 0
    private var latestZoom = 0
    private let maxRunning = 6
    private let cache: NSCache<NSURL, NSData> = {
        let c = NSCache<NSURL, NSData>()
        c.totalCostLimit = 120 * 1024 * 1024
        return c
    }()
    private let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.httpMaximumConnectionsPerHost = 6
        c.timeoutIntervalForRequest = 30
        return URLSession(configuration: c)
    }()

    func load(_ url: URL, zoom: Int, for overlay: QueuedTileOverlay,
              result: @escaping @Sendable (Data?, (any Error)?) -> Void) {
        if let hit = cache.object(forKey: url as NSURL) {
            result(hit as Data, nil)
            return
        }
        lock.withLock {
            counter += 1
            latestZoom = zoom
            waiting.append(Job(url: url, zoom: zoom, order: counter, overlay: overlay, result: result))
        }
        startJobs()
    }

    /// The overlay left the map: its waiting tiles will never be drawn.
    func cancel(for overlay: QueuedTileOverlay) {
        let dropped = lock.withLock {
            let mine = waiting.filter { $0.overlay === overlay }
            waiting.removeAll { $0.overlay === overlay }
            return mine
        }
        for job in dropped { job.result(nil, URLError(.cancelled)) }
    }

    private func startJobs() {
        while let job = nextJob() {
            session.dataTask(with: job.url) { [self] data, response, error in
                if let data, (response as? HTTPURLResponse)?.statusCode == 200 {
                    cache.setObject(data as NSData, forKey: job.url as NSURL, cost: data.count)
                    job.result(data, nil)
                } else {
                    job.result(nil, error ?? URLError(.badServerResponse))
                }
                lock.withLock { running -= 1 }
                startJobs()
            }.resume()
        }
    }

    private func nextJob() -> Job? {
        lock.withLock { () -> Job? in
            guard running < maxRunning, !waiting.isEmpty else { return nil }
            func rank(_ j: Job) -> (Int, Int, Int) { (j.zoom == latestZoom ? 1 : 0, j.overlay.onScreen ? 1 : 0, j.order) }
            let best = waiting.indices.max { rank(waiting[$0]) < rank(waiting[$1]) }!
            running += 1
            return waiting.remove(at: best)
        }
    }
}
