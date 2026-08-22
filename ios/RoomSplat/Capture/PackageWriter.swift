// Record-to-disk debug mode writer (SPEC.md M1 gate, §6).
//
// Writes a complete, self-contained `.roomsplat` directory directly to local disk
// with no network dependency. The layout is byte-identical in structure to the server's
// streaming disk mirror:
//
//     <uuid>.roomsplat/
//     ├── capture.json          # session metadata matching §6 schema
//     ├── transforms.json       # Nerfstudio-style camera model + keyframe poses
//     ├── thumbnail.jpg         # first keyframe JPEG
//     ├── images/
//     │   ├── frame_00000.jpg   # keyframe images
//     │   └── ...
//     └── points3D.ply          # fused LiDAR cloud, binary little-endian XYZRGB
//
// Exportable via the Files app (UIFileSharingEnabled / LSSupportsOpeningDocumentsInPlace).

import ARKit
import Foundation
import simd

final class PackageWriter {
    let sessionID: String
    let packageURL: URL
    let imagesURL: URL

    private var camera: [String: Any]?
    private var frames: [[String: Any]] = []
    private var isClosed = false
    private let fileManager = FileManager.default

    // Session metadata tracking
    var deviceModel: String = UIDevice.current.model
    var capturedAt: String = ISO8601DateFormatter().string(from: Date())
    var exposureLocked: Bool = true
    var whiteBalanceLocked: Bool = true
    var trackingWarnings: [String] = []
    var thermalEvents: [[String: Any]] = []
    var backpressureEvents: Int = 0
    var seedVoxelSizeM: Float = 0.02

    /// Initialize a new PackageWriter at the given parent directory (e.g. Documents).
    init(sessionID: String = UUID().uuidString, in parentURL: URL? = nil) {
        self.sessionID = sessionID
        let base = parentURL ?? fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
        self.packageURL = base.appendingPathComponent("\(sessionID).roomsplat", isDirectory: true)
        self.imagesURL = packageURL.appendingPathComponent("images", isDirectory: true)
    }

    /// Create package directories and initialize metadata files.
    func open(with frame: ARFrame) throws {
        try fileManager.createDirectory(at: imagesURL, withIntermediateDirectories: true)

        let intr = frame.camera.intrinsics
        let res = frame.camera.imageResolution
        self.camera = [
            "camera_model": "OPENCV",
            "fl_x": Double(intr.columns.0.x),
            "fl_y": Double(intr.columns.1.y),
            "cx": Double(intr.columns.2.x),
            "cy": Double(intr.columns.2.y),
            "w": Int(res.width),
            "h": Int(res.height),
        ]

        try writeCaptureJSON(seedPointCount: 0)
        try writeTransformsJSON()
    }

    /// Append one keyframe: writes `images/frame_XXXXX.jpg` and atomically updates `transforms.json`.
    func appendKeyframe(index: Int, jpegData: Data, cameraToWorld: simd_float4x4) throws {
        guard !isClosed else { return }

        let fileName = String(format: "frame_%05d.jpg", index)
        let fileURL = imagesURL.appendingPathComponent(fileName)
        try jpegData.write(to: fileURL, options: .atomic)

        // Write thumbnail if not already present
        let thumbURL = packageURL.appendingPathComponent("thumbnail.jpg")
        if !fileManager.fileExists(atPath: thumbURL.path) {
            try jpegData.write(to: thumbURL, options: .atomic)
        }

        let relPath = "images/\(fileName)"
        frames.append([
            "file_path": relPath,
            "transform_matrix": cameraToWorld.rowMajorValues,
        ])

        try writeTransformsJSON()
    }

    /// Write the fused LiDAR point cloud to `points3D.ply` in binary little-endian format.
    func writePointCloud(points: [(position: SIMD3<Float>, color: SIMD3<UInt8>)]) throws {
        let plyURL = packageURL.appendingPathComponent("points3D.ply")
        let data = encodePLY(points: points)
        try data.write(to: plyURL, options: .atomic)
    }

    /// Finalize the package: writes final metadata, point cloud, and closes.
    func finish(pointCloud: [(position: SIMD3<Float>, color: SIMD3<UInt8>)] = []) throws {
        guard !isClosed else { return }
        isClosed = true

        if !pointCloud.isEmpty {
            try writePointCloud(points: pointCloud)
        }

        try writeCaptureJSON(seedPointCount: pointCloud.count)
        try writeTransformsJSON()
    }

    // MARK: - JSON Writers

    private func writeCaptureJSON(seedPointCount: Int) throws {
        let captureDoc: [String: Any] = [
            "schema_version": 2,
            "session_id": sessionID,
            "device_model": deviceModel,
            "captured_at": capturedAt,
            "source": "debug",
            "frame_count": frames.count,
            "frames_dropped": 0,
            "exposure_locked": exposureLocked,
            "white_balance_locked": whiteBalanceLocked,
            "tracking_warnings": trackingWarnings,
            "thermal_events": thermalEvents,
            "backpressure_events": backpressureEvents,
            "preview_trained_on_device": false,
            "seed_point_count": seedPointCount,
            "seed_voxel_size_m": seedVoxelSizeM,
        ]

        let data = try JSONSerialization.data(withJSONObject: captureDoc, options: [.prettyPrinted, .sortedKeys])
        let captureURL = packageURL.appendingPathComponent("capture.json")
        try data.write(to: captureURL, options: .atomic)
    }

    private func writeTransformsJSON() throws {
        guard let camera else { return }
        var transformsDoc = camera
        transformsDoc["ply_file_path"] = "points3D.ply"
        transformsDoc["frames"] = frames

        let data = try JSONSerialization.data(withJSONObject: transformsDoc, options: [.prettyPrinted, .sortedKeys])
        let transformsURL = packageURL.appendingPathComponent("transforms.json")
        try data.write(to: transformsURL, options: .atomic)
    }

    // MARK: - Binary PLY Encoding (SPEC.md §6)

    /// Encode point cloud as binary_little_endian 1.0 PLY matching server/roomsplat/package.py.
    private func encodePLY(points: [(position: SIMD3<Float>, color: SIMD3<UInt8>)]) -> Data {
        let header = "ply\nformat binary_little_endian 1.0\nelement vertex \(points.count)\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"

        var data = Data(header.utf8)
        data.reserveCapacity(data.count + points.count * 15) // 12 bytes xyz + 3 bytes rgb

        for pt in points {
            var x = pt.position.x
            var y = pt.position.y
            var z = pt.position.z
            withUnsafeBytes(of: &x) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: &y) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: &z) { data.append(contentsOf: $0) }
            data.append(pt.color.x)
            data.append(pt.color.y)
            data.append(pt.color.z)
        }

        return data
    }
}
