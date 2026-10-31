import SwiftUI

struct ContentView: View {
    @State private var model = RadarModel()

    var body: some View {
        Group {
            if model.phase == .ready {
                RadarScreen(model: model)
            } else {
                ConnectScreen(model: model)
            }
        }
        .task { await model.start() }
    }
}

/// The radar: full-screen map, with the info panel, legend and control bar on top.
///
/// Siri Remote:
///   Play/Pause → animate · bar → product, zoom, home, map style
///   "Move map" → arrows/swipes pan, click zooms in, click-and-hold zooms out, Back returns to the bar
///   Back from the bar → hide everything (wall display); any press brings it back; Back again exits
struct RadarScreen: View {
    let model: RadarModel
    @State private var chrome = true
    @State private var moving = false
    @FocusState private var focus: Target?

    enum Target: Hashable { case play, map, wake }

    var body: some View {
        ZStack(alignment: .bottom) {
            RadarMapView(layers: model.layers, currentLayerID: model.currentLayerID, opacity: model.opacity,
                         look: model.look, camera: model.camera, home: model.home) { s, w, n, e in
                model.regionChanged(south: s, west: w, north: n, east: e)
            }
            .ignoresSafeArea()

            if moving {
                MoveMapLayer(model: model, focus: $focus) { leaveMoveMode() }
            } else if chrome {
                VStack(alignment: .leading, spacing: 24) {
                    HStack(alignment: .top) {
                        InfoPanel(model: model)
                        Spacer()
                        MapTools(model: model) {
                            moving = true
                            focus = .map
                        }
                    }
                    Spacer()
                    if let legend = model.legend { LegendView(legend: legend) }
                    ProductBar(model: model, focus: $focus)
                }
                .padding(.horizontal, 60)
                .padding(.vertical, 40)
                .frame(maxWidth: .infinity, alignment: .leading)
                .transition(.opacity)
            } else {
                // Invisible catcher: any press brings the controls back.
                Color.clear
                    .contentShape(Rectangle())
                    .focusable()
                    .focused($focus, equals: .wake)
                    .onTapGesture { showChrome() }
                    .onMoveCommand { _ in showChrome() }
                    .ignoresSafeArea()
            }
        }
        .onPlayPauseCommand { model.togglePlay() }
        .onExitCommand(perform: exitAction)
        .onAppear { focus = .play }
        .animation(.easeInOut(duration: 0.25), value: chrome)
        .animation(.easeInOut(duration: 0.25), value: moving)
    }

    /// Back: leave move mode → hide controls → (nil) let tvOS leave the app.
    private var exitAction: (() -> Void)? {
        if moving { return { leaveMoveMode() } }
        if chrome { return { chrome = false; focus = .wake } }
        return nil
    }

    private func leaveMoveMode() {
        moving = false
        focus = .play
    }

    private func showChrome() {
        chrome = true
        focus = .play
    }
}

// MARK: - Panels

struct InfoPanel: View {
    let model: RadarModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("REALLYMYRADAR")
                .font(.caption2.weight(.semibold))
                .tracking(3)
                .foregroundStyle(.secondary)
            Text(Product.name(model.product))
                .font(.title3.weight(.semibold))
            Text(model.sourceLabel)
                .font(.callout)
                .foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                Text(model.timeText)
                    .font(.system(size: 44, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                Text(model.ageText)
                    .font(.callout.weight(.bold))
                    .foregroundStyle(model.liveFrame ? Color.green : .secondary)
            }
            FrameTicks(count: model.layers.count, current: model.frameIndex)
        }
        .padding(28)
        .frame(width: 560, alignment: .leading)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 28, style: .continuous))
    }
}

struct FrameTicks: View {
    let count: Int
    let current: Int

    var body: some View {
        HStack(spacing: 6) {
            ForEach(0..<count, id: \.self) { i in
                Capsule()
                    .fill(i == current ? Color.accentColor : Color.white.opacity(0.25))
                    .frame(height: 8)
            }
        }
        .frame(height: 8)
    }
}

struct LegendView: View {
    let legend: Legend

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(legend.units.isEmpty ? legend.title : "\(legend.title) · \(legend.units)")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            LinearGradient(stops: legend.marks.map { .init(color: $0.color, location: $0.location) },
                           startPoint: .leading, endPoint: .trailing)
                .frame(width: 520, height: 16)
                .clipShape(Capsule())
            HStack {
                ForEach(Array(legend.ticks.enumerated()), id: \.offset) { i, t in
                    Text(t)
                    if i < legend.ticks.count - 1 { Spacer() }
                }
            }
            .font(.caption2.monospacedDigit())
            .foregroundStyle(.secondary)
            .frame(width: 520)
        }
        .padding(22)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

/// Bottom row: play/pause and the radar products.
struct ProductBar: View {
    let model: RadarModel
    var focus: FocusState<RadarScreen.Target?>.Binding

    var body: some View {
        HStack(spacing: 16) {
            Button { model.togglePlay() } label: {
                Image(systemName: model.playing ? "pause.fill" : "play.fill")
                    .frame(width: 40)
            }
            .focused(focus, equals: .play)

            ForEach(model.products, id: \.self) { code in
                ProductButton(code: code, selected: code == model.product) { model.select(product: code) }
            }
        }
        .font(.callout.weight(.semibold))
    }
}

/// Top-right: moving and zooming the map, home, map style.
struct MapTools: View {
    let model: RadarModel
    let startMoving: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            Button(action: startMoving) { Image(systemName: "arrow.up.and.down.and.arrow.left.and.right") }
            Button { model.zoom(in: false) } label: { Image(systemName: "minus.magnifyingglass") }
            Button { model.zoom(in: true) } label: { Image(systemName: "plus.magnifyingglass") }
            Button { model.goHome() } label: { Image(systemName: "house.fill") }
            Button { model.cycleLook() } label: { Image(systemName: "map") }
        }
        .font(.callout.weight(.semibold))
    }
}

struct ProductButton: View {
    let code: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(Product.short(code))
                .lineLimit(1)
                .fixedSize()
                .foregroundStyle(selected ? Color.accentColor : .primary)
                .overlay(alignment: .bottom) {
                    Capsule()
                        .fill(selected ? Color.accentColor : .clear)
                        .frame(height: 4)
                        .offset(y: 12)
                }
        }
    }
}

// MARK: - Moving the map

struct MoveMapLayer: View {
    let model: RadarModel
    var focus: FocusState<RadarScreen.Target?>.Binding
    let done: () -> Void

    var body: some View {
        ZStack(alignment: .top) {
            Color.clear
                .contentShape(Rectangle())
                .focusable()
                .focused(focus, equals: .map)
                .onMoveCommand { model.pan($0) }
                .onTapGesture { model.zoom(in: true) }
                .onLongPressGesture(minimumDuration: 0.6) { model.zoom(in: false) }
                .ignoresSafeArea()

            HStack(spacing: 34) {
                hint("arrow.up.and.down.and.arrow.left.and.right", "Arrows or swipe to move")
                hint("plus.magnifyingglass", "Click to zoom in")
                hint("minus.magnifyingglass", "Click and hold to zoom out")
                hint("playpause.fill", "Play / pause")
                hint("arrow.uturn.backward", "Back when done")
            }
            .font(.callout.weight(.medium))
            .padding(.horizontal, 34)
            .padding(.vertical, 22)
            .background(.ultraThinMaterial, in: Capsule())
            .padding(.top, 50)
            .allowsHitTesting(false)
        }
    }

    private func hint(_ symbol: String, _ text: LocalizedStringKey) -> some View {
        Label(text, systemImage: symbol)
    }
}

// MARK: - Finding the server

struct ConnectScreen: View {
    let model: RadarModel
    @State private var address = ""

    var body: some View {
        VStack(spacing: 36) {
            Image(systemName: "dot.radiowaves.left.and.right")
                .font(.system(size: 110, weight: .light))
                .foregroundStyle(Color.accentColor)
                .symbolEffect(.pulse, isActive: isSearching)
            Text(title).font(.title2.weight(.semibold))
            Text("Make sure the ReallymyRadar app is running on a computer on this network. It's found automatically.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 1100)
            if case let .failed(message) = model.phase {
                Text(message).font(.callout).foregroundStyle(.orange)
            }
            HStack(spacing: 20) {
                TextField("Or type its address, like 192.168.1.20", text: $address)
                    .frame(width: 820)
                    .onSubmit(connect)
                Button("Connect", action: connect)
            }
            if case .failed = model.phase {
                Button("Search again") { model.discover() }
            }
        }
        .padding(80)
    }

    private var isSearching: Bool {
        if case .searching = model.phase { return true }
        return false
    }

    private var title: String {
        switch model.phase {
        case .searching: "Looking for ReallymyRadar on your network…"
        case let .connecting(host): "Connecting to \(host)…"
        case .failed: "Couldn't connect"
        case .ready: "Connected"
        }
    }

    private func connect() {
        let text = address
        Task { await model.useAddress(text) }
    }
}
