// Ported verbatim from the reference iOS capture writer (SPEC.md §5).
//
// Source: vendor/ios-gaussian-splatting-demo/3DGS Demo/Capture/GaussianCaptureView.swift
//   - `extension simd_float4x4 { var rowMajorValues }`
//   - the seed-point back-projection in `copySeedPoints`
//
// The reference repo is PolyForm Noncommercial 1.0.0; see THIRD_PARTY_NOTICES.md in
// this directory. Do NOT re-derive this from the handedness table in §5 and do NOT
// "clean it up". ARKit already uses the Nerfstudio/OpenGL convention (right-handed,
// +Y up, camera looks -Z), which is what gsplat's parser expects, so there is no
// handedness flip here. Adding one produces a mirrored room (§5 diagnostic table).

import simd

extension simd_float4x4 {
    /// ARKit camera-to-world (column-major) -> row-major values for transforms.json.
    var rowMajorValues: [[Float]] {
        (0..<4).map { row in
            (0..<4).map { column in self[column][row] }
        }
    }
}

enum CoordinateTransform {
    /// Back-project a depth sample to a world-space point. Note the y flip
    /// `(cy - imageY)` and the -z camera forward, matching ARKit's -Z look direction.
    static func worldPoint(
        imageX: Float,
        imageY: Float,
        depth z: Float,
        intrinsics: simd_float3x3,
        cameraToWorld: simd_float4x4
    ) -> SIMD3<Float> {
        let fx = intrinsics.columns.0.x
        let fy = intrinsics.columns.1.y
        let cx = intrinsics.columns.2.x
        let cy = intrinsics.columns.2.y
        let cameraX = (imageX - cx) * z / fx
        let cameraY = (cy - imageY) * z / fy
        let world = cameraToWorld * SIMD4<Float>(cameraX, cameraY, -z, 1)
        return SIMD3<Float>(world.x, world.y, world.z)
    }
}
