// Drives the ARSession and wires the reviewed pieces together (SPEC.md M1/M3):
// KeyframeSelector -> IngestClient over the single ingest WebSocket, ThermalGovernor
// for the required thermal policy, PointCloudFuser for the seed LiDAR cloud.
//
// ARSession delegate callbacks run on a background queue; all published UI state is
// hopped to the main queue.

import ARKit
import AVFoundation
import CoreImage
import ImageIO
import UIKit
import simd

final class CaptureCoordinator: NSObject, ObservableObject {
    enum Link {
        case offline      // not capturing / socket down
        case connecting   // capturing, socket handshake not yet complete
        case connected    // socket open, no frame sent yet
        case transmitting // frames leaving the socket successfully
        case error        // a send failed
    }

    enum CaptureMode: String, CaseIterable, Identifiable {
        case stream = "Stream"
        case recordToDisk = "Record to Disk"

        var id: String { rawValue }
    }

    @Published var captureMode: CaptureMode = .stream
    @Published private(set) var isCapturing = false
    @Published private(set) var keyframeCount = 0
    @Published private(set) var pointCount = 0
    @Published private(set) var link: Link = .offline
    @Published private(set) var thermalState: ProcessInfo.ThermalState = .nominal
    @Published private(set) var status = "Idle"
    /// True while ARKit is delivering LiDAR sceneDepth on the current frames.
    @Published private(set) var lidarActive = false
    /// Number of ARKit scene-reconstruction mesh anchors captured so far.
    @Published private(set) var meshAnchorCount = 0
    /// Show the ARKit LiDAR mesh wireframe overlay on the camera preview.
    @Published var showMesh = true {
        didSet { meshVisualizer?.isHidden = !showMesh }
    }
    @Published var serverHost: String {
        didSet { UserDefaults.standard.set(serverHost, forKey: "serverHost") }
    }

    static var deviceSupported: Bool {
        ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    }

    /// Map a user-entered host to a WebSocket URL, preserving any explicit scheme
    /// (https/wss -> wss, http/ws -> ws). A bare host defaults to **wss://** so App
    /// Transport Security is satisfied against a public TLS endpoint like
    /// sea.octo80.com; only obviously-local hosts (loopback, .local, RFC1918, or
    /// Tailscale CGNAT 100.64/10) fall back to cleartext ws://, which additionally
    /// requires NSAllowsLocalNetworking in the app's ATS settings. Trailing slashes
    /// are trimmed so appendingPathComponent doesn't produce a double slash.
    static func normalizeWebSocketURL(_ host: String) -> String {
        var s = host.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        let lower = s.lowercased()
        if lower.hasPrefix("wss://") || lower.hasPrefix("ws://") { return s }
        if lower.hasPrefix("https://") { return "wss://" + s.dropFirst("https://".count) }
        if lower.hasPrefix("http://") { return "ws://" + s.dropFirst("http://".count) }
        return (isLocalHost(lower) ? "ws://" : "wss://") + s
    }

    /// Heuristic for hosts that won't have a publicly-trusted TLS cert and so use ws://.
    static func isLocalHost(_ host: String) -> Bool {
        let name = host.split(separator: ":").first.map(String.init) ?? host
        if name == "localhost" || name.hasSuffix(".local") { return true }
        if !name.contains(".") { return true }              // bare mDNS name, e.g. "inception"
        let o = name.split(separator: ".").compactMap { Int($0) }
        guard o.count == 4 else { return false }            // not an IPv4 literal -> public DNS
        if o[0] == 127 || o[0] == 10 { return true }
        if o[0] == 192 && o[1] == 168 { return true }
        if o[0] == 172 && (16...31).contains(o[1]) { return true }
        if o[0] == 100 && (64...127).contains(o[1]) { return true } // Tailscale CGNAT
        return false
    }

    private weak var arSession: ARSession?
    private var selector = KeyframeSelector()
    private var ingest: IngestClient?
    private var packageWriter: PackageWriter?
    private let governor = ThermalGovernor()
    private let fuser = PointCloudFuser()
    private let ciContext = CIContext(options: [.cacheIntermediates: false])
    private let jpegColorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
    private static let jpegLongEdge: CGFloat = 1600  // SPEC.md §6
    // Live preview for the viewer PiP: small + frequent, independent of the keyframe gate
    // so the PiP stays responsive even when the operator holds still.
    private static let previewLongEdge: CGFloat = 640
    private static let previewInterval: TimeInterval = 0.25  // ~4 fps
    private var lastPreviewTime: TimeInterval = 0
    // Rotation to make the preview upright for the viewer PiP. The ARKit capturedImage is
    // always in the native landscape-right sensor frame; this maps the live device
    // orientation to the CIImage orientation that displays it the way the operator holds
    // the phone. Only the preview is rotated — training keyframes stay native (§5).
    private var captureOrientation: CGImagePropertyOrientation = .right
    private var orientationObserver: NSObjectProtocol?
    // ARKit scene-reconstruction mesh anchors, streamed to the viewer's LiDAR tab.
    private var meshAnchors: [UUID: ARMeshAnchor] = [:]
    private var meshVisualizer: MeshVisualizer?
    private var lidarActiveInternal = false
    private var lastMeshTime: TimeInterval = 0
    // Send the room mesh as soon as ARKit changes it (meshDirty), throttled to at most
    // once per meshMinInterval so a growing room doesn't flood the link. This keeps the
    // web LiDAR view in step with what the phone is drawing instead of ~3 s behind.
    private var meshDirty = true
    private static let meshMinInterval: TimeInterval = 0.75

    // Camera locks (SPEC.md §3): lock exposure + white balance, fixed focus pre-session.
    // Assert every frame; record a warning in session metadata if any lock is lost.
    private var captureDevice: AVCaptureDevice?
    private(set) var exposureLocked = false
    private(set) var whiteBalanceLocked = false
    private(set) var focusLocked = false
    private var trackingWarnings: [String] = []

    private var frameIndex = 0
    private var lastCloudKeyframe = 0
    private var cloudSent = false
    private var sessionOpened = false
    // Stable per-capture id + the exact session_open payload, so a reconnect resends
    // session_open with the SAME id and the server resumes rather than restarts (§4).
    private var sessionId = ""
    private var pendingSessionOpen: [String: Any]?
    private var latestCloud: Data?
    private var minKeyframeInterval: TimeInterval = 0
    private var lastKeyframeTime: TimeInterval = 0
    private var lastError: String?
    private let queue = DispatchQueue(label: "roomsplat.capture")

    override init() {
        serverHost = UserDefaults.standard.string(forKey: "serverHost") ?? ""
        super.init()
        governor.onPolicyChange = { [weak self] policy, state in
            self?.queue.async { self?.handlePolicy(policy, state) }
        }
    }

    func attach(_ view: ARSCNView) {
        arSession = view.session
        view.session.delegate = self
        view.session.delegateQueue = queue
        meshVisualizer = MeshVisualizer(scnView: view)
        meshVisualizer?.isHidden = !showMesh
    }

    func start() {
        guard Self.deviceSupported else { setStatus("No LiDAR / sceneDepth on this device"); return }
        guard let arSession else { setStatus("No ARSession"); return }
        meshVisualizer?.clear()

        if captureMode == .stream {
            let host = serverHost.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !host.isEmpty else { setStatus("Enter a server host first"); return }
            let urlString = Self.normalizeWebSocketURL(host)
            guard let url = URL(string: urlString) else {
                setStatus("Bad server host"); return
            }
            print("[capture] starting stream; server host=\(host) -> \(url.absoluteString)")

            queue.async {
                self.selector = KeyframeSelector()
                self.fuser.reset()
                self.frameIndex = 0
                self.lastCloudKeyframe = 0
                self.cloudSent = false
                self.sessionOpened = false
                self.sessionId = UUID().uuidString
                self.pendingSessionOpen = nil
                self.latestCloud = nil
                self.lastKeyframeTime = 0
                self.meshAnchors = [:]
                self.lastMeshTime = 0
                self.meshDirty = true
                self.packageWriter = nil

                let client = IngestClient(serverURL: url)
                client.onBackpressure = { [weak self] in
                    self?.queue.async { self?.selector.escalateForBackpressure() }
                }
                client.onConnectionChange = { [weak self] up in
                    self?.queue.async { self?.handleConnection(up) }
                }
                client.onTransmit = { [weak self] ok in
                    self?.queue.async { self?.handleTransmit(ok) }
                }
                client.onError = { [weak self] message in
                    self?.queue.async { self?.handleError(message) }
                }
                client.onStageAssignment = { [weak self] assignment in
                    self?.queue.async { self?.handleStageAssignment(assignment) }
                }
                client.connect()
                self.ingest = client
            }

            publish {
                self.keyframeCount = 0
                self.pointCount = 0
                self.isCapturing = true
                self.link = .connecting
                self.status = "Connecting to \(self.serverHost)…"
            }
        } else {
            // Record-to-disk debug mode (SPEC.md M1)
            print("[capture] starting record-to-disk debug mode")
            queue.async {
                self.selector = KeyframeSelector()
                self.fuser.reset()
                self.frameIndex = 0
                self.lastCloudKeyframe = 0
                self.cloudSent = false
                self.sessionOpened = false
                self.lastKeyframeTime = 0
                self.ingest = nil
                self.packageWriter = PackageWriter()
            }

            publish {
                self.keyframeCount = 0
                self.pointCount = 0
                self.isCapturing = true
                self.link = .offline
                self.status = "Recording to disk…"
            }
        }

        let config = ARWorldTrackingConfiguration()
        config.frameSemantics = .sceneDepth
        config.worldAlignment = .gravity
        // Scene reconstruction gives a triangle mesh of the room for the viewer's LiDAR
        // tab (in addition to the fused point cloud used for splat seeding).
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) {
            config.sceneReconstruction = .mesh
        }
        arSession.run(config, options: [.resetTracking, .removeExistingAnchors])
        applyCameraLocks()
        governor.start()
        startOrientationTracking()
    }

    private func startOrientationTracking() {
        DispatchQueue.main.async {
            UIDevice.current.beginGeneratingDeviceOrientationNotifications()
            self.captureOrientation = Self.previewOrientation(for: UIDevice.current.orientation)
            self.orientationObserver = NotificationCenter.default.addObserver(
                forName: UIDevice.orientationDidChangeNotification, object: nil, queue: .main
            ) { [weak self] _ in
                self?.captureOrientation = Self.previewOrientation(for: UIDevice.current.orientation)
            }
        }
    }

    private func stopOrientationTracking() {
        DispatchQueue.main.async {
            if let obs = self.orientationObserver {
                NotificationCenter.default.removeObserver(obs)
                self.orientationObserver = nil
            }
            UIDevice.current.endGeneratingDeviceOrientationNotifications()
        }
    }

    /// Map device orientation to the CIImage orientation that makes the native
    /// landscape-right capturedImage display upright. Only affects the preview PiP.
    private static func previewOrientation(for device: UIDeviceOrientation) -> CGImagePropertyOrientation {
        switch device {
        case .portrait: return .right
        case .portraitUpsideDown: return .left
        case .landscapeLeft: return .up        // device rotated left → sensor already upright
        case .landscapeRight: return .down
        default: return .right                 // faceUp/faceDown/unknown: assume portrait
        }
    }

    func stop() {
        arSession?.pause()
        unlockCamera()
        governor.stop()
        stopOrientationTracking()
        queue.async {
            if let ingest = self.ingest {
                ingest.send(control: .sessionComplete, payload: [:])
                self.ingest = nil
            }
            if let writer = self.packageWriter {
                do {
                    try writer.finish(pointCloud: self.fuser.points)
                    print("[capture] finished writing package to \(writer.packageURL.path)")
                } catch {
                    print("[capture] failed to finish package: \(error)")
                }
                self.packageWriter = nil
            }
            self.lidarActiveInternal = false
        }
        meshVisualizer?.clear()
        publish {
            self.isCapturing = false
            self.link = .offline
            self.status = "Stopped"
            self.lidarActive = false
        }
    }

    // MARK: - connection state

    private func handleConnection(_ up: Bool) {
        guard isCapturingNow else { return }
        if up { lastError = nil }
        // On a (re)connect after the session was already opened, re-send session_open
        // with the same id so the server resumes this session on the new socket, then
        // reseed the cloud. Without this, frames on the new socket are dropped as
        // "before session_open" while the UI still shows Transmitting (SPEC.md §4).
        if up, let payload = pendingSessionOpen, let ingest {
            ingest.send(control: .sessionOpen, payload: payload)
            ingest.send(control: .capabilityReport, payload: ["lidar": true, "scene_depth": true])
            if let cloud = latestCloud { ingest.sendPointCloud(cloud) }
        }
        let detail = lastError.map { " (\($0))" } ?? ""
        publish {
            switch self.link {
            case .transmitting where up:
                break
            default:
                self.link = up ? .connected : .connecting
                self.status = up ? "Connected, waiting for frames…" : "Reconnecting…\(detail)"
            }
        }
    }

    private func handleTransmit(_ ok: Bool) {
        guard isCapturingNow else { return }
        if ok { lastError = nil }
        let detail = lastError.map { ": \($0)" } ?? ""
        publish {
            self.link = ok ? .transmitting : .error
            self.status = ok ? "Transmitting" : "Send failed\(detail)"
        }
    }

    private func handleError(_ message: String) {
        guard isCapturingNow else { return }
        lastError = message
        publish {
            self.link = .error
            self.status = "Link error: \(message)"
        }
    }

    private var isCapturingNow: Bool { ingest != nil }

    // MARK: - session lifecycle

    private func openSession(with frame: ARFrame, ingest: IngestClient) {
        let intr = frame.camera.intrinsics
        let res = frame.camera.imageResolution
        let formatter = ISO8601DateFormatter()
        let payload: [String: Any] = [
            "session_id": sessionId,
            "device_model": UIDevice.current.model,
            "captured_at": formatter.string(from: Date()),
            "exposure_locked": exposureLocked,
            "white_balance_locked": whiteBalanceLocked,
            "tracking_warnings": trackingWarnings,
            "camera": [
                "camera_model": "OPENCV",
                "fl_x": Double(intr.columns.0.x),
                "fl_y": Double(intr.columns.1.y),
                "cx": Double(intr.columns.2.x),
                "cy": Double(intr.columns.2.y),
                "w": Int(res.width),
                "h": Int(res.height),
            ],
        ]
        pendingSessionOpen = payload  // remembered so a reconnect can resume this session
        ingest.send(control: .sessionOpen, payload: payload)
        ingest.send(control: .capabilityReport, payload: [
            "device_model": UIDevice.current.model,
            "lidar": true,
            "scene_depth": true,
            "thermal_state": Self.name(for: ProcessInfo.processInfo.thermalState),
            "preview_training_supported": true,
        ])
    }

    private func handleStageAssignment(_ assignment: [String: Any]) {
        print("[capture] server stage assignment: \(assignment)")
        // Honor server stage assignment (SPEC.md §2):
        // preview_training, coverage_analysis, keyframe_selection, lidar_fusion
        if let previewEnabled = assignment["preview_training"] as? Bool {
            print("[capture] on-device preview training assigned: \(previewEnabled)")
        }
    }

    private func handlePolicy(_ policy: WorkPolicy, _ state: ProcessInfo.ThermalState) {
        switch policy {
        case .full, .suspendPreview: minKeyframeInterval = 0
        case .degrade: minKeyframeInterval = 0.5  // 2 keyframes/s (SPEC.md §2)
        }
        ingest?.send(control: .thermalState, payload: [
            "t": Date().timeIntervalSince1970,
            "state": Self.name(for: state),
        ])
        publish { self.thermalState = state }
    }

    private static func name(for state: ProcessInfo.ThermalState) -> String {
        switch state {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }

    private func jpegData(from pixelBuffer: CVPixelBuffer) -> Data? {
        var image = CIImage(cvPixelBuffer: pixelBuffer)
        // Downscale to a 1600 px long edge (SPEC.md §6) BEFORE encoding. The old path
        // rendered a full-resolution CGImage and encoded it with UIImage.jpegData on the
        // capture queue, which stalled the keyframe loop (slow transmission + laggy PiP).
        // Scaling first plus CIContext.jpegRepresentation cuts both encode time and bytes.
        let longEdge = max(image.extent.width, image.extent.height)
        if longEdge > Self.jpegLongEdge {
            let s = Self.jpegLongEdge / longEdge
            image = image.transformed(by: CGAffineTransform(scaleX: s, y: s))
        }
        let options: [CIImageRepresentationOption: Any] = [
            CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.8
        ]
        return ciContext.jpegRepresentation(of: image, colorSpace: jpegColorSpace, options: options)
    }

    /// Small, low-quality JPEG for the live PiP (not a training keyframe): 640 px long
    /// edge at q0.5, so ~4 fps costs little bandwidth.
    private func previewJpeg(from pixelBuffer: CVPixelBuffer) -> Data? {
        // Rotate to match how the phone is held so the PiP is upright (preview only).
        var image = CIImage(cvPixelBuffer: pixelBuffer).oriented(captureOrientation)
        let longEdge = max(image.extent.width, image.extent.height)
        if longEdge > Self.previewLongEdge {
            let s = Self.previewLongEdge / longEdge
            image = image.transformed(by: CGAffineTransform(scaleX: s, y: s))
        }
        let options: [CIImageRepresentationOption: Any] = [
            CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.5
        ]
        return ciContext.jpegRepresentation(of: image, colorSpace: jpegColorSpace, options: options)
    }

    private func setStatus(_ text: String) { publish { self.status = text } }

    // MARK: - camera configuration & lock assertion (SPEC.md §3)

    /// Configure and lock exposure, white balance, and focus before capture begins.
    /// Auto-exposure drift while panning past a window causes blotchy splats; autofocus
    /// alters intrinsics mid-capture.
    private func applyCameraLocks() {
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            print("[capture] no back wide-angle camera available for locking")
            return
        }
        captureDevice = device
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }

            // 1. Exposure: lock to current exposure settings
            if device.isExposureModeSupported(.locked) {
                device.exposureMode = .locked
                exposureLocked = true
                print("[capture] exposure locked")
            } else {
                exposureLocked = false
                recordWarning("Exposure lock not supported on device")
            }

            // 2. White balance: lock to current gains
            if device.isWhiteBalanceModeSupported(.locked) {
                device.whiteBalanceMode = .locked
                whiteBalanceLocked = true
                print("[capture] white balance locked")
            } else {
                whiteBalanceLocked = false
                recordWarning("White balance lock not supported on device")
            }

            // 3. Focus: lock to current lens position (fixed focus)
            if device.isFocusModeSupported(.locked) {
                device.focusMode = .locked
                focusLocked = true
                print("[capture] focus locked")
            } else {
                focusLocked = false
                recordWarning("Focus lock not supported on device")
            }
        } catch {
            print("[capture] failed to lock camera configuration: \(error)")
            recordWarning("Camera lock configuration failed: \(error.localizedDescription)")
        }
    }

    /// Restore continuous auto modes on capture device when session stops.
    private func unlockCamera() {
        guard let device = captureDevice else { return }
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            if device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposureMode = .continuousAutoExposure
            }
            if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                device.whiteBalanceMode = .continuousAutoWhiteBalance
            }
            if device.isFocusModeSupported(.continuousAutoFocus) {
                device.focusMode = .continuousAutoFocus
            }
        } catch {
            print("[capture] failed to unlock camera: \(error)")
        }
        exposureLocked = false
        whiteBalanceLocked = false
        focusLocked = false
        captureDevice = nil
    }

    /// Assert every frame that camera locks remain intact. Record a tracking warning if lost.
    private func assertCameraLocks() {
        guard let device = captureDevice else { return }
        if exposureLocked && device.exposureMode != .locked {
            exposureLocked = false
            recordWarning("Exposure lock lost during capture")
        }
        if whiteBalanceLocked && device.whiteBalanceMode != .locked {
            whiteBalanceLocked = false
            recordWarning("White balance lock lost during capture")
        }
        if focusLocked && device.focusMode != .locked {
            focusLocked = false
            recordWarning("Focus lock lost during capture")
        }
    }

    /// Record a warning in session metadata and forward to server via WebSocket if connected.
    private func recordWarning(_ message: String) {
        print("[capture warning] \(message)")
        trackingWarnings.append(message)
        ingest?.send(control: .trackingWarning, payload: ["message": message])
    }

    private func publish(_ work: @escaping () -> Void) {
        DispatchQueue.main.async(execute: work)
    }
}

extension CaptureCoordinator: ARSessionDelegate {
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        updateLidarState(frame)
        // Assert camera locks every frame (SPEC.md §3): exposure, WB, fixed focus
        assertCameraLocks()

        if let ingest = self.ingest {
            handleStreamUpdate(frame: frame, ingest: ingest)
        } else if let writer = self.packageWriter {
            handleDiskUpdate(frame: frame, writer: writer)
        }
    }

    private func handleStreamUpdate(frame: ARFrame, ingest: IngestClient) {
        if !sessionOpened {
            openSession(with: frame, ingest: ingest)
            sessionOpened = true
            fuser.integrate(frame: frame)
            // sceneDepth is often nil during ARKit warmup; only send a non-empty seed.
            sendCloudIfReady(ingest, keyframe: 0)
        }

        // Live preview on its own cadence, BEFORE the movement-gated keyframe checks, so
        // the viewer PiP stays responsive even when the operator holds still.
        if frame.timestamp - lastPreviewTime >= Self.previewInterval {
            lastPreviewTime = frame.timestamp
            if let preview = previewJpeg(from: frame.capturedImage) {
                ingest.sendPreview(preview)
            }
        }

        // Stream the ARKit room mesh whenever it changed, throttled, so the web LiDAR view
        // tracks what the phone is drawing rather than lagging a fixed 3 s behind.
        if meshDirty, !meshAnchors.isEmpty, frame.timestamp - lastMeshTime >= Self.meshMinInterval {
            lastMeshTime = frame.timestamp
            meshDirty = false
            if let mesh = RoomMeshEncoder.encode(Array(meshAnchors.values)) {
                ingest.sendRoomMesh(mesh)
            }
        }

        if minKeyframeInterval > 0, frame.timestamp - lastKeyframeTime < minKeyframeInterval {
            return
        }

        let blur = BlurScore.laplacianVariance(frame.capturedImage)
        guard selector.shouldAccept(frame, blurScore: blur) else { return }
        lastKeyframeTime = frame.timestamp

        guard let jpeg = jpegData(from: frame.capturedImage) else { return }
        let index = frameIndex
        frameIndex += 1
        ingest.sendKeyframe(index: index, jpeg: jpeg,
                            cameraToWorld: frame.camera.transform, timestamp: frame.timestamp)

        fuser.integrate(frame: frame)
        let accepted = selector.acceptedCount
        // Send the seed as soon as it has points, then resend every 50 keyframes (§3).
        if !cloudSent || accepted - lastCloudKeyframe >= 50 {
            sendCloudIfReady(ingest, keyframe: accepted)
        }

        let pc = fuser.count
        publish {
            self.keyframeCount = accepted
            self.pointCount = pc
        }
    }

    private func handleDiskUpdate(frame: ARFrame, writer: PackageWriter) {
        if !sessionOpened {
            do {
                try writer.open(with: frame)
                writer.exposureLocked = exposureLocked
                writer.whiteBalanceLocked = whiteBalanceLocked
                writer.trackingWarnings = trackingWarnings
                sessionOpened = true
            } catch {
                print("[capture] failed to open package writer: \(error)")
                return
            }
        }

        if minKeyframeInterval > 0, frame.timestamp - lastKeyframeTime < minKeyframeInterval {
            return
        }

        let blur = BlurScore.laplacianVariance(frame.capturedImage)
        guard selector.shouldAccept(frame, blurScore: blur) else { return }
        lastKeyframeTime = frame.timestamp

        guard let jpeg = jpegData(from: frame.capturedImage) else { return }
        let index = frameIndex
        frameIndex += 1

        do {
            try writer.appendKeyframe(index: index, jpegData: jpeg, cameraToWorld: frame.camera.transform)
        } catch {
            print("[capture] failed to append keyframe to package: \(error)")
        }

        fuser.integrate(frame: frame)
        let accepted = selector.acceptedCount
        let pc = fuser.count
        publish {
            self.keyframeCount = accepted
            self.pointCount = pc
        }
    }

    /// Send the fused cloud only when it is non-empty, so the server never seeds a
    /// trainer from zero points during depth warmup.
    private func sendCloudIfReady(_ ingest: IngestClient, keyframe: Int) {
        guard fuser.count > 0 else { return }
        let encoded = fuser.encode()
        latestCloud = encoded  // kept so a reconnect can reseed the trainer/viewer
        ingest.sendPointCloud(encoded)
        cloudSent = true
        lastCloudKeyframe = keyframe
        let pc = fuser.count
        publish { self.pointCount = pc }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        setStatus("AR error: \(error.localizedDescription)")
    }

    // Collect ARKit scene-reconstruction mesh anchors for the LiDAR room view. These
    // callbacks run on the same background queue as didUpdate, so mutation is serial.
    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) {
        for case let m as ARMeshAnchor in anchors {
            meshAnchors[m.identifier] = m
            meshVisualizer?.update(m)
            meshDirty = true
        }
        publishMeshCount()
    }
    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) {
        for case let m as ARMeshAnchor in anchors {
            meshAnchors[m.identifier] = m
            meshVisualizer?.update(m)
            meshDirty = true
        }
        publishMeshCount()
    }
    func session(_ session: ARSession, didRemove anchors: [ARAnchor]) {
        for case let m as ARMeshAnchor in anchors {
            meshAnchors.removeValue(forKey: m.identifier)
            meshVisualizer?.remove(m.identifier)
            meshDirty = true
        }
        publishMeshCount()
    }

    /// Publish LiDAR liveness only when it flips, not every frame.
    private func updateLidarState(_ frame: ARFrame) {
        let active = frame.sceneDepth != nil
        guard active != lidarActiveInternal else { return }
        lidarActiveInternal = active
        publish { self.lidarActive = active }
    }

    private func publishMeshCount() {
        let count = meshAnchors.count
        publish { self.meshAnchorCount = count }
    }
}
