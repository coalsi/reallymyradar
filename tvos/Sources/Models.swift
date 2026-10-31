import SwiftUI

// Shapes of the ReallymyRadar server's JSON (decoded with .convertFromSnakeCase).

struct ServerConfig: Decodable {
    struct Home: Decodable, Equatable {
        var lat: Double?
        var lon: Double?
        var label: String?
    }
    struct Render: Decodable {
        var styles: [String: ProductStyle]
        var velocityUnits: String
    }
    struct Settings: Decodable {
        var render: Render
    }
    var home: Home
    var products: [String]
    var settings: Settings
    var palettes: [String: Palette]
}

struct ProductStyle: Decodable, Equatable {
    var palette: String
    var vmin: Double
    var vmax: Double
    var opacity: Double
}

struct Palette: Decodable, Equatable {
    /// Encoded by the server as `[value, "#rrggbb", alpha]`.
    struct Stop: Decodable, Equatable {
        var value: Double
        var hex: String
        var alpha: Double

        init(from decoder: Decoder) throws {
            var c = try decoder.unkeyedContainer()
            value = try c.decode(Double.self)
            hex = try c.decode(String.self)
            alpha = try c.decode(Double.self)
        }
    }
    var units: String
    var stops: [Stop]
}

struct Frame: Decodable, Equatable, Identifiable {
    var id: String
    var time: String
    var rev: String
    var complete: Bool
}

struct MosaicResponse: Decodable {
    struct Member: Decodable { var id: String; var frames: Int }
    var frames: [Frame]
    var members: [Member]
    var loading: Bool
}

struct SiteResponse: Decodable {
    var site: String
    var frames: [Frame]
    var loading: Bool
}

struct NearSite: Decodable { var id: String }

// MARK: - What the views draw

/// One animation frame's radar tiles, as a MapKit tile-URL template.
struct RadarLayer: Equatable, Identifiable {
    let id: String
    let urlTemplate: String
}

struct Legend: Equatable {
    struct Mark: Equatable { let location: Double; let color: Color }
    let title: String
    let units: String
    let marks: [Mark]
    let ticks: [String]
}

struct HomePoint: Equatable {
    let latitude: Double
    let longitude: Double
}

enum MapLook: String, CaseIterable, Equatable {
    case dark, hybrid, satellite

    var title: String {
        switch self {
        case .dark: "Dark map"
        case .hybrid: "Hybrid"
        case .satellite: "Satellite"
        }
    }

    var next: MapLook {
        let all = MapLook.allCases
        return all[(all.firstIndex(of: self)! + 1) % all.count]
    }
}

/// A one-shot camera move the map view applies once (identified by `id`).
struct CameraRequest: Equatable {
    enum Kind: Equatable {
        case pan(dx: Double, dy: Double)
        case zoom(factor: Double)
        case home
    }
    let id: Int
    let kind: Kind
}

enum Product {
    static let mosaicable: Set<String> = ["CREF", "REF", "ZDR", "CC"]

    static func name(_ code: String) -> String {
        switch code {
        case "CREF": "Composite Reflectivity"
        case "REF": "Base Reflectivity"
        case "VEL": "Velocity"
        case "SW": "Spectrum Width"
        case "ZDR": "Differential Reflectivity"
        case "CC": "Correlation Coefficient"
        case "PHI": "Differential Phase"
        default: code
        }
    }

    static func short(_ code: String) -> String {
        switch code {
        case "CREF": "Composite"
        case "REF": "Reflectivity"
        case "VEL": "Velocity"
        case "SW": "Width"
        case "ZDR": "ZDR"
        case "CC": "CC"
        case "PHI": "Phase"
        default: code
        }
    }
}

extension Color {
    init(hex: String, opacity: Double = 1) {
        let v = UInt32(hex.trimmingCharacters(in: CharacterSet(charactersIn: "#")), radix: 16) ?? 0
        self.init(.sRGB, red: Double((v >> 16) & 0xFF) / 255, green: Double((v >> 8) & 0xFF) / 255,
                  blue: Double(v & 0xFF) / 255, opacity: opacity)
    }
}
