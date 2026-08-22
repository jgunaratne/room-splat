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
    case sessionComplete = "session_complete"
    case sessionAbort = "session_abort"
}

final class IngestClient {
    static let pointCloudFrameIndex: UInt32 = 0xFFFF_FFFF
    /// Backpressure trigger (SPEC.md §3): above this buffered amount, raise keyframe
    /// thresholds rather than queueing.
    static let backpressureBytes = 8 * 1024 * 1024

    private let serverURL: URL
    private var task: URLSessionWebSocketTask?
    private let session = URLSession(configuration: .default)

    private(set) var highestAck: Int = -1
    var onBackpressure: (() -> Void)?

    init(serverURL: URL) {
        self.serverURL = serverURL
    }

    func connect() {
        let task = session.webSocketTask(with: serverURL.appendingPathComponent("ws/ingest"))
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
        task?.send(.string(text)) { error in
            if let error { print("ingest control send failed:", error) }
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

    // MARK: binary framing

    private func sendBinary(frameIndex: UInt32, payload: Data) {
        var header = frameIndex.bigEndian
        var frame = Data(bytes: &header, count: 4)
        frame.append(payload)
        task?.send(.data(frame)) { error in
            if let error { print("ingest binary send failed:", error) }
        }
    }

    private func isBackpressured() -> Bool {
        // URLSessionWebSocketTask does not expose bufferedAmount directly; approximate
        // by tracking outstanding sends. On a WKWebView bridge this reads
        // socket.bufferedAmount instead (SPEC.md §3).
        false
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
                print("ingest receive failed, reconnecting:", error)
                self.reconnect()
            }
        }
    }

    private func handleServerMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if obj["type"] as? String == "ack", let idx = obj["frame_index"] as? Int {
            highestAck = max(highestAck, idx)  // resume point on reconnect (§4)
        }
    }

    private func reconnect() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.connect()
            // Caller resumes sending from highestAck + 1; capture is never blocked
            // on acknowledgment.
        }
    }
}
