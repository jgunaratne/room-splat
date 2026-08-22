// Keyframe acceptance rules (SPEC.md M1) with M3 backpressure escalation.
//
// Adapted from the reference `shouldAccept` (see CoordinateTransform.swift for the
// license note), with the spec's thresholds substituted: reject unless the camera has
// moved >= 15 cm OR rotated >= 10 deg since the last accepted frame; reject if the
// blur score is below the running median of accepted frames; reject unless tracking is
// .normal; cap at 300 keyframes.

import ARKit
import simd

struct KeyframeSelector {
    static let maxKeyframes = 300

    // Base thresholds; raised under backpressure so a redundant frame is dropped
    // rather than queued (SPEC.md §3).
    private let baseTranslation: Float = 0.15   // 15 cm
    private let baseRotation: Float = 0.1745    // 10 deg in radians

    private var translationThreshold: Float
    private var rotationThreshold: Float
    private var previousPose: simd_float4x4?
    private var acceptedBlurScores: [Float] = []
    private(set) var acceptedCount = 0
    private(set) var backpressureEscalations = 0

    init() {
        translationThreshold = baseTranslation
        rotationThreshold = baseRotation
    }

    /// Raise thresholds when the upload buffer exceeds 8 MB (SPEC.md §3). Log every
    /// escalation at the call site.
    mutating func escalateForBackpressure() {
        translationThreshold = min(translationThreshold * 1.5, 0.6)
        rotationThreshold = min(rotationThreshold * 1.5, 0.7)
        backpressureEscalations += 1
    }

    mutating func relaxThresholds() {
        translationThreshold = baseTranslation
        rotationThreshold = baseRotation
    }

    /// Decide whether to accept `frame`. `blurScore` is the variance-of-Laplacian on a
    /// downscaled grayscale copy (computed by the caller, not the full frame).
    mutating func shouldAccept(_ frame: ARFrame, blurScore: Float) -> Bool {
        guard acceptedCount < Self.maxKeyframes else { return false }
        guard case .normal = frame.camera.trackingState else { return false }

        if let previousPose {
            let oldP = SIMD3<Float>(previousPose.columns.3.x, previousPose.columns.3.y, previousPose.columns.3.z)
            let newP = SIMD3<Float>(frame.camera.transform.columns.3.x,
                                    frame.camera.transform.columns.3.y,
                                    frame.camera.transform.columns.3.z)
            let translation = simd_distance(oldP, newP)

            let oldF = -SIMD3<Float>(previousPose.columns.2.x, previousPose.columns.2.y, previousPose.columns.2.z)
            let newF = -SIMD3<Float>(frame.camera.transform.columns.2.x,
                                     frame.camera.transform.columns.2.y,
                                     frame.camera.transform.columns.2.z)
            let cosine = simd_clamp(simd_dot(simd_normalize(oldF), simd_normalize(newF)), -1, 1)
            let angle = acosf(cosine)

            guard translation >= translationThreshold || angle >= rotationThreshold else {
                return false
            }
        }

        // Blur gate: reject below the running median of accepted frames.
        if !acceptedBlurScores.isEmpty {
            let sorted = acceptedBlurScores.sorted()
            let median = sorted[sorted.count / 2]
            guard blurScore >= median else { return false }
        }

        previousPose = frame.camera.transform
        acceptedBlurScores.append(blurScore)
        acceptedCount += 1
        return true
    }
}
