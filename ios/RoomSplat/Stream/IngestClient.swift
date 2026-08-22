// Live upload client over the single ingest WebSocket (SPEC.md §3, §4).
//
// One socket carries both control (JSON text frames) and payload (binary frames:
// [4-byte big-endian frame_index][JPEG bytes]). Each image is preceded by its
// keyframe_meta text frame. The fused LiDAR cloud is a binary frame with the
// POINT_CLOUD sentinel index, sent after session_open and resent every 50 keyframes.
//
// Invariants: the stream is lossy by design (a dropped frame is a logged warning);
// reconnect resumes from the highest acknowledged frame_index, never restarts.

import Foundation
import simd

enum IngestControl: String, Encodable {
    case sessionOpen = "session_open"
    case keyframeMeta = "keyframe_meta"
    case capabilityReport = "capability_report"
    case thermalState = "thermal_state"
    case trackingWarning = "tracking_warning"
    case sessionComplete = "session_complete"
    case sessionAbort = "session_abort"
}

final class IngestClient: NSObject, URLSessionWebSocketDelegate {
    static let pointCloudFrameIndex: UInt32 = 0xFFFF_FFFF
    static let previewFrameIndex: UInt32 = 0xFFFF_FFFE
    static let meshFrameIndex: UInt32 = 0xFFFF_FFFD
    /// Backpressure trigger (SPEC.md §3): above this buffered amount, raise keyframe
    /// thresholds rather than queueing.
    static let backpressureBytes = 8 * 1024 * 1024

    /// Ping cadence. Cloudflare (and most proxies) close idle WebSockets after ~100 s;
    /// keyframes can be sparser than that when the operator holds still, so ping to
    /// keep the tunnel open.
    private static let keepaliveInterval: TimeInterval = 30

    private let serverURL: URL
    private var task: URLSessionWebSocketTask?
    private var pingTimer: DispatchSourceTimer?
    private lazy var session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)

    private(set) var highestAck: Int = -1
    var onBackpressure: (() -> Void)?
    /// Socket handshake completed (true) or closed/failed (false).
    var onConnectionChange: ((Bool) -> Void)?
    /// Outcome of the most recent frame send: true if it left the socket, false on error.
    var onTransmit: ((Bool) -> Void)?
    /// Human-readable reason a connection failed (handshake error, TLS, drop), for the UI.
    var onError: ((String) -> Void)?
    /// Stage assignment from the server (SPEC.md §2 capability negotiation).
    var onStageAssignment: (([String: Any]) -> Void)?

    init(serverURL: URL) {
        self.serverURL = serverURL
        super.init()
    }

    func connect() {
        let url = serverURL.appendingPathComponent("ws/ingest")
        print("[ingest] connecting to \(url.absoluteString)")
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        receiveLoop()
    }

    // MARK: control

    func send(control type: IngestControl, payload: [String: Any]) {
        var obj = payload
        obj["type"] = type.rawValue
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              let text = String(data: data, encoding: .utf8) else { return }
        task?.send(.string(text)) { [weak self] error in
            if let error { print("[ingest] control send failed:", error) }
            self?.onTransmit?(error == nil)
        }
    }

    /// Send a keyframe: its meta text frame immediately followed by the JPEG binary
    /// frame. Returns false if backpressure is active (caller should have escalated).
    @discardableResult
    func sendKeyframe(index: Int, jpeg: Data, cameraToWorld: simd_float4x4, timestamp: TimeInterval) -> Bool {
        if isBackpressured() {
            onBackpressure?()
            return false
        }
        send(control: .keyframeMeta, payload: [
            "frame_index": index,
            "transform_matrix": cameraToWorld.rowMajorValues,
            "timestamp": timestamp,
        ])
        sendBinary(frameIndex: UInt32(index), payload: jpeg)
        return true
    }

    func sendPointCloud(_ data: Data) {
        sendBinary(frameIndex: Self.pointCloudFrameIndex, payload: data)
    }

    /// ARKit scene-reconstruction room mesh for the viewer's LiDAR tab.
    func sendRoomMesh(_ data: Data) {
        sendBinary(frameIndex: Self.meshFrameIndex, payload: data)
    }

    /// Low-res preview frame for the viewer PiP. Best-effort: skipped under backpressure
    /// and never acked, so it never competes with keyframes on a congested link.
    func sendPreview(_ data: Data) {
        guard !isBackpressured() else { return }
        sendBinary(frameIndex: Self.previewFrameIndex, payload: data)
    }

    private let queue = DispatchQueue(label: "roomsplat.ingest.client")
    private var pendingBytes: Int = 0

    // MARK: binary framing

    private func sendBinary(frameIndex: UInt32, payload: Data) {
        var header = frameIndex.bigEndian
        var frame = Data(bytes: &header, count: 4)
        frame.append(payload)
        let byteCount = frame.count

        queue.sync {
            pendingBytes += byteCount
            if pendingBytes > Self.backpressureBytes {
                DispatchQueue.main.async { [weak self] in
                    self?.onBackpressure?()
                }
            }
        }

        task?.send(.data(frame)) { [weak self] error in
            guard let self else { return }
            self.queue.sync {
                self.pendingBytes = max(0, self.pendingBytes - byteCount)
            }
            if let error { print("[ingest] binary send failed:", error) }
            self.onTransmit?(error == nil)
        }
    }

    private func isBackpressured() -> Bool {
        queue.sync { pendingBytes > Self.backpressureBytes }
    }

    // MARK: receive (acks + reconnect)

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(.string(let text)):
                self.handleServerMessage(text)
                self.receiveLoop()
            case .success:
                self.receiveLoop()
            case .failure(let error):
                print("[ingest] receive failed, reconnecting:", error)
                self.stopKeepalive()
                self.onError?(error.localizedDescription)
                self.onConnectionChange?(false)
                self.reconnect()
            }
        }
    }

    private func handleServerMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let type = obj["type"] as? String
        if type == "ack", let idx = obj["frame_index"] as? Int {
            highestAck = max(highestAck, idx)  // resume point on reconnect (§4)
        } else if type == "stage_assignment" {
            print("[ingest] received stage_assignment from server:", obj)
            onStageAssignment?(obj)
        }
    }

    private func reconnect() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.connect()
            // HighestAck is preserved so stream continues without restarting (§4)
        }
    }

    // MARK: URLSessionWebSocketDelegate (handshake state)

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        print("[ingest] connected (handshake open)")
        startKeepalive()
        onConnectionChange?(true)
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        stopKeepalive()
        let text = reason.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        print("[ingest] closed code=\(closeCode.rawValue) reason=\(text.isEmpty ? "<none>" : text)")
        if !text.isEmpty {
            onError?("closed (\(closeCode.rawValue)): \(text)")
        } else {
            onError?("closed (code \(closeCode.rawValue))")
        }
        onConnectionChange?(false)
    }

    /// Fires when the underlying task ends — including a handshake that never opened
    /// (e.g. Cloudflare returns 403/1006 or a TLS error), which `didClose` won't report.
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error {
            print("[ingest] task failed: \(error.localizedDescription) — \(error)")
            onError?(error.localizedDescription)
            onConnectionChange?(false)
        }
    }

    // MARK: keepalive

    private func startKeepalive() {
        stopKeepalive()
        let timer = DispatchSource.makeTimerSource(queue: .global())
        timer.schedule(deadline: .now() + Self.keepaliveInterval, repeating: Self.keepaliveInterval)
        timer.setEventHandler { [weak self] in
            self?.task?.sendPing { error in
                if let error { print("[ingest] ping failed:", error) }
            }
        }
        timer.resume()
        pingTimer = timer
    }

    private func stopKeepalive() {
        pingTimer?.cancel()
        pingTimer = nil
    }
}
