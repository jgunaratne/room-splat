// Fused LiDAR point cloud from ARKit sceneDepth (SPEC.md §3 seed cloud).
//
// A coarse first pass of the M1 fusion: back-project each confident depth sample with
// CoordinateTransform, voxel-downsample at 2 cm, cap at 500k points. Encoded in the
// trivial wire format the server and viewer already parse (AGENTS.md decision 5,
// train/export.py): [uint32 count][count x (float32 x,y,z, uint8 r,g,b)], little-endian.

import ARKit
import CoreImage
import simd

final class PointCloudFuser {
    private let voxelSize: Float = 0.02
    private let maxPoints = 500_000
    private let maxDepth: Float = 5.0

    private var voxels: [SIMD3<Int32>: SIMD3<UInt8>] = [:]
    private let ciContext = CIContext(options: nil)

    var count: Int { voxels.count }

    func reset() { voxels.removeAll(keepingCapacity: true) }

    func integrate(frame: ARFrame) {
        guard voxels.count < maxPoints, let sceneDepth = frame.sceneDepth else { return }
        let depth = sceneDepth.depthMap
        let confidence = sceneDepth.confidenceMap
        let dw = CVPixelBufferGetWidth(depth)
        let dh = CVPixelBufferGetHeight(depth)

        guard let rgba = bgraBytes(from: frame.capturedImage, width: dw, height: dh) else { return }

        // ARKit intrinsics are for the full capturedImage; scale them to the depth map.
        let res = frame.camera.imageResolution
        let sx = Float(dw) / Float(res.width)
        let sy = Float(dh) / Float(res.height)
        let intr = frame.camera.intrinsics
        var scaled = matrix_identity_float3x3
        scaled.columns.0.x = intr.columns.0.x * sx
        scaled.columns.1.y = intr.columns.1.y * sy
        scaled.columns.2.x = intr.columns.2.x * sx
        scaled.columns.2.y = intr.columns.2.y * sy
        let cameraToWorld = frame.camera.transform

        CVPixelBufferLockBaseAddress(depth, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depth, .readOnly) }
        if let confidence { CVPixelBufferLockBaseAddress(confidence, .readOnly) }
        defer { if let confidence { CVPixelBufferUnlockBaseAddress(confidence, .readOnly) } }

        guard let depthBase = CVPixelBufferGetBaseAddress(depth) else { return }
        let depthStride = CVPixelBufferGetBytesPerRow(depth) / MemoryLayout<Float32>.size
        let depthPtr = depthBase.assumingMemoryBound(to: Float32.self)

        let confPtr = confidence.flatMap(CVPixelBufferGetBaseAddress)?.assumingMemoryBound(to: UInt8.self)
        let confStride = confidence.map(CVPixelBufferGetBytesPerRow) ?? 0

        let inv = 1 / voxelSize
        for y in 0..<dh {
            for x in 0..<dw {
                if let confPtr, confPtr[y * confStride + x] < ARConfidenceLevel.medium.rawValue {
                    continue
                }
                let z = depthPtr[y * depthStride + x]
                guard z > 0, z < maxDepth else { continue }

                let world = CoordinateTransform.worldPoint(
                    imageX: Float(x), imageY: Float(y), depth: z,
                    intrinsics: scaled, cameraToWorld: cameraToWorld
                )
                let key = SIMD3<Int32>(Int32((world.x * inv).rounded(.down)),
                                       Int32((world.y * inv).rounded(.down)),
                                       Int32((world.z * inv).rounded(.down)))
                if voxels[key] == nil {
                    guard voxels.count < maxPoints else { return }
                    let i = (y * dw + x) * 4
                    voxels[key] = SIMD3<UInt8>(rgba[i], rgba[i + 1], rgba[i + 2])
                }
            }
        }
    }

    /// Retrieve the current fused points as (position, color) tuples.
    var points: [(position: SIMD3<Float>, color: SIMD3<UInt8>)] {
        voxels.map { key, color in
            let pos = SIMD3<Float>(
                (Float(key.x) + 0.5) * voxelSize,
                (Float(key.y) + 0.5) * voxelSize,
                (Float(key.z) + 0.5) * voxelSize
            )
            return (position: pos, color: color)
        }
    }

    /// Encode the current cloud in the little-endian wire format (device is little-endian).
    func encode() -> Data {
        var data = Data()
        var count = UInt32(voxels.count)
        appendRaw(&count, to: &data)
        for (key, color) in voxels {
            var x = (Float(key.x) + 0.5) * voxelSize
            var y = (Float(key.y) + 0.5) * voxelSize
            var z = (Float(key.z) + 0.5) * voxelSize
            appendRaw(&x, to: &data)
            appendRaw(&y, to: &data)
            appendRaw(&z, to: &data)
            data.append(color.x)
            data.append(color.y)
            data.append(color.z)
        }
        return data
    }

    private func appendRaw<T>(_ value: inout T, to data: inout Data) {
        withUnsafeBytes(of: &value) { data.append(contentsOf: $0) }
    }

    /// Captured image (YCbCr) scaled to depth resolution as RGBA bytes for color sampling.
    private func bgraBytes(from pixelBuffer: CVPixelBuffer, width: Int, height: Int) -> [UInt8]? {
        let ci = CIImage(cvPixelBuffer: pixelBuffer)
        let scale = CGAffineTransform(scaleX: CGFloat(width) / ci.extent.width,
                                      y: CGFloat(height) / ci.extent.height)
        let scaled = ci.transformed(by: scale)
        guard let cg = ciContext.createCGImage(scaled, from: CGRect(x: 0, y: 0, width: width, height: height)) else {
            return nil
        }
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        guard let ctx = CGContext(
            data: &bytes, width: width, height: height, bitsPerComponent: 8,
            bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return nil }
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: width, height: height))
        return bytes
    }
}
