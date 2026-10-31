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
        private var overlays: [String: MKTileOverlay] = [:]
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
                let overlay = CachedTileOverlay(urlTemplate: layer.urlTemplate)
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
                overlays[id] = nil
                renderers[id] = nil
            }
            visibleID = current
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

/// Tile overlay with a shared in-memory cache. MapKit's own tile cache is small and the animation
/// cycles through every frame, so without this the same tiles are downloaded over and over.
nonisolated final class CachedTileOverlay: MKTileOverlay {
    // NSCache is documented as thread-safe; MapKit calls loadTile from its own threads.
    nonisolated(unsafe) private static let cache: NSCache<NSURL, NSData> = {
        let c = NSCache<NSURL, NSData>()
        c.totalCostLimit = 120 * 1024 * 1024
        return c
    }()

    override func loadTile(at path: MKTileOverlayPath, result: @escaping @Sendable (Data?, (any Error)?) -> Void) {
        let url = self.url(forTilePath: path)
        if let hit = Self.cache.object(forKey: url as NSURL) {
            result(hit as Data, nil)
            return
        }
        URLSession.shared.dataTask(with: url) { data, response, error in
            if let data, (response as? HTTPURLResponse)?.statusCode == 200 {
                Self.cache.setObject(data as NSData, forKey: url as NSURL, cost: data.count)
            }
            result(data, error)
        }.resume()
    }
}
