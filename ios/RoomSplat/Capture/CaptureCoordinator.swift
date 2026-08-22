// Drives the ARSession and wires the reviewed pieces together (SPEC.md M1/M3):
// KeyframeSelector -> IngestClient over the single ingest WebSocket, ThermalGovernor
// for the required thermal policy, PointCloudFuser for the seed LiDAR cloud.
//
// ARSession delegate callbacks run on a background queue; all published UI state is
// hopped to the main queue.

import ARKit
import CoreImage
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

    @Published private(set) var isCapturing = false
    @Published private(set) var keyframeCount = 0
    @Published private(set) var pointCount = 0
    @Published private(set) var link: Link = .offline
    @Published private(set) var thermalState: ProcessInfo.ThermalState = .nominal
    @Published private(set) var status = "Idle"
    @Published var serverHost: String {
        didSet { UserDefaults.standard.set(serverHost, forKey: "serverHost") }
    }

    static var deviceSupported: Bool {
        ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    }

    /// Map a user-entered host to a WebSocket URL: https->wss, http->ws, bare host->ws,
    /// preserving any explicit ws(s):// scheme. Trailing slashes are trimmed.
    static func normalizeWebSocketURL(_ host: String) -> String {
        var s = host.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        let lower = s.lowercased()
        if lower.hasPrefix("wss://") || lower.hasPrefix("ws://") {
            return s
        } else if lower.hasPrefix("https://") {
            return "wss://" + s.dropFirst("https://".count)
        } else if lower.hasPrefix("http://") {
            return "ws://" + s.dropFirst("http://".count)
        }
        return "ws://" + s
    }

    private weak var arSession: ARSession?
    private var selector = KeyframeSelector()
    private var ingest: IngestClient?
    private let governor = ThermalGovernor()
    private let fuser = PointCloudFuser()
    private let ciContext = CIContext(options: nil)

    private var frameIndex = 0
    private var lastCloudKeyframe = 0
    private var cloudSent = false
    private var sessionOpened = false
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

    func attach(_ session: ARSession) {
        arSession = session
        session.delegate = self
        session.delegateQueue = queue
    }

    func start() {
        let host = serverHost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty else { setStatus("Enter a server host first"); return }
        guard Self.deviceSupported else { setStatus("No LiDAR / sceneDepth on this device"); return }
        // URLSessionWebSocketTask only accepts ws:// or wss:// — normalize a pasted
        // http(s):// endpoint (e.g. https://sea.octo80.com) to the WebSocket scheme,
        // and default a bare host to ws://. Trailing slashes are stripped so
        // appendingPathComponent("ws/ingest") doesn't produce a double slash.
        let urlString = Self.normalizeWebSocketURL(host)
        guard let arSession, let url = URL(string: urlString) else {
            setStatus("Bad server host"); return
        }
        print("[capture] starting; server host=\(host) -> \(url.absoluteString)")

        queue.async {
            self.selector = KeyframeSelector()
            self.fuser.reset()
            self.frameIndex = 0
            self.lastCloudKeyframe = 0
            self.cloudSent = false
            self.sessionOpened = false
            self.lastKeyframeTime = 0

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
            client.connect()
            self.ingest = client
        }

        let config = ARWorldTrackingConfiguration()
        config.frameSemantics = .sceneDepth
        config.worldAlignment = .gravity
        arSession.run(config, options: [.resetTracking, .removeExistingAnchors])
        governor.start()

        publish {
            self.keyframeCount = 0
            self.pointCount = 0
            self.isCapturing = true
            self.link = .connecting
            self.status = "Connecting to \(self.serverHost)…"
        }
    }

    func stop() {
        arSession?.pause()
        governor.stop()
        queue.async {
            self.ingest?.send(control: .sessionComplete, payload: [:])
            self.ingest = nil
        }
        publish {
            self.isCapturing = false
            self.link = .offline
            self.status = "Stopped"
        }
    }

    // MARK: - connection state

    private func handleConnection(_ up: Bool) {
        guard isCapturingNow else { return }
        if up { lastError = nil }
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
        ingest.send(control: .sessionOpen, payload: [
            "session_id": UUID().uuidString,
            "device_model": UIDevice.current.model,
            "captured_at": formatter.string(from: Date()),
            "camera": [
                "camera_model": "OPENCV",
                "fl_x": Double(intr.columns.0.x),
                "fl_y": Double(intr.columns.1.y),
                "cx": Double(intr.columns.2.x),
                "cy": Double(intr.columns.2.y),
                "w": Int(res.width),
                "h": Int(res.height),
            ],
        ])
        ingest.send(control: .capabilityReport, payload: ["lidar": true, "scene_depth": true])
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
        let ci = CIImage(cvPixelBuffer: pixelBuffer)
        guard let cg = ciContext.createCGImage(ci, from: ci.extent) else { return nil }
        return UIImage(cgImage: cg).jpegData(compressionQuality: 0.7)
    }

    private func setStatus(_ text: String) { publish { self.status = text } }

    private func publish(_ work: @escaping () -> Void) {
        DispatchQueue.main.async(execute: work)
    }
}

extension CaptureCoordinator: ARSessionDelegate {
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard let ingest else { return }

        if !sessionOpened {
            openSession(with: frame, ingest: ingest)
            sessionOpened = true
            fuser.integrate(frame: frame)
            // sceneDepth is often nil during ARKit warmup; only send a non-empty seed.
            sendCloudIfReady(ingest, keyframe: 0)
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

    /// Send the fused cloud only when it is non-empty, so the server never seeds a
    /// trainer from zero points during depth warmup.
    private func sendCloudIfReady(_ ingest: IngestClient, keyframe: Int) {
        guard fuser.count > 0 else { return }
        ingest.sendPointCloud(fuser.encode())
        cloudSent = true
        lastCloudKeyframe = keyframe
        let pc = fuser.count
        publish { self.pointCount = pc }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        setStatus("AR error: \(error.localizedDescription)")
    }
}
