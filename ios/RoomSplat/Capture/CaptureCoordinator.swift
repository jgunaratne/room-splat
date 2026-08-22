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
    @Published private(set) var isCapturing = false
    @Published private(set) var keyframeCount = 0
    @Published private(set) var pointCount = 0
    @Published private(set) var connected = false
    @Published private(set) var thermalState: ProcessInfo.ThermalState = .nominal
    @Published private(set) var status = "Idle"
    @Published var serverHost: String {
        didSet { UserDefaults.standard.set(serverHost, forKey: "serverHost") }
    }

    static var deviceSupported: Bool {
        ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    }

    private weak var arSession: ARSession?
    private var selector = KeyframeSelector()
    private var ingest: IngestClient?
    private let governor = ThermalGovernor()
    private let fuser = PointCloudFuser()
    private let ciContext = CIContext(options: nil)

    private var frameIndex = 0
    private var lastCloudKeyframe = 0
    private var sessionOpened = false
    private var minKeyframeInterval: TimeInterval = 0
    private var lastKeyframeTime: TimeInterval = 0
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
        guard !serverHost.isEmpty else { setStatus("Enter a server host first"); return }
        guard Self.deviceSupported else { setStatus("No LiDAR / sceneDepth on this device"); return }
        guard let arSession, let url = URL(string: "ws://\(serverHost)") else {
            setStatus("Bad server host"); return
        }

        queue.async {
            self.selector = KeyframeSelector()
            self.fuser.reset()
            self.frameIndex = 0
            self.lastCloudKeyframe = 0
            self.sessionOpened = false
            self.lastKeyframeTime = 0

            let client = IngestClient(serverURL: url)
            client.onBackpressure = { [weak self] in
                self?.queue.async { self?.selector.escalateForBackpressure() }
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
            self.connected = true
            self.status = "Capturing"
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
            self.connected = false
            self.status = "Stopped"
        }
    }

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
            ingest.sendPointCloud(fuser.encode())
            let pc = fuser.count
            publish { self.pointCount = pc }
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
        if accepted - lastCloudKeyframe >= 50 {
            lastCloudKeyframe = accepted
            ingest.sendPointCloud(fuser.encode())
        }

        let pc = fuser.count
        publish {
            self.keyframeCount = accepted
            self.pointCount = pc
        }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        setStatus("AR error: \(error.localizedDescription)")
    }
}
