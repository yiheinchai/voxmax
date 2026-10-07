// swift-tools-version:5.10
import PackageDescription

// Hotspot Guard desktop app. Liquid Glass needs the macOS 26 SDK, so the minimum is macOS 26.
// Build the .app bundle with ./build-app.sh (it also bundles the Python engine).
let package = Package(
    name: "HotspotGuard",
    platforms: [.macOS("26.0")],
    targets: [
        .executableTarget(
            name: "HotspotGuard",
            path: "Sources/HotspotGuard"
        )
    ]
)
