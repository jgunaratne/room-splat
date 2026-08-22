// Thermal governor (SPEC.md §2 — required, not optional).
//
// Sustained ARKit + LiDAR + camera + Metal training + upload will thermally throttle
// an iPhone within minutes, and throttling degrades ARKit tracking, which corrupts
// poses, which ruins the splat. Capture and upload are NEVER shed — they are the only
// irreplaceable work.

import Foundation

enum WorkPolicy {
    case full            // .nominal / .fair
    case suspendPreview  // .serious
    case degrade         // .critical: drop keyframe rate to 2/s, notify operator
}

final class ThermalGovernor {
    /// Called whenever the policy changes. Emit a thermal_state control message and,
    /// for .critical, notify the operator.
    var onPolicyChange: ((WorkPolicy, ProcessInfo.ThermalState) -> Void)?

    private(set) var policy: WorkPolicy = .full
    private var observer: NSObjectProtocol?

    func start() {
        apply(ProcessInfo.processInfo.thermalState)
        observer = NotificationCenter.default.addObserver(
            forName: ProcessInfo.thermalStateDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.apply(ProcessInfo.processInfo.thermalState)
        }
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
    }

    private func apply(_ state: ProcessInfo.ThermalState) {
        let next: WorkPolicy
        switch state {
        case .nominal, .fair: next = .full
        case .serious: next = .suspendPreview  // suspend preview training; keep streaming
        case .critical: next = .degrade        // 2/s keyframes; keep streaming
        @unknown default: next = .suspendPreview
        }
        guard next != policy else { return }
        policy = next
        onPolicyChange?(next, state)
    }
}

extension WorkPolicy: Equatable {}
