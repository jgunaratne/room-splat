// Encodes ARKit scene-reconstruction mesh anchors into the room.bin wire format the
// viewer's LiDAR tab renders (see web/src/roommesh.js):
//
//   [uint32 vertexCount][uint32 indexCount]
//   [vertexCount x float32 x,y,z][vertexCount x float32 nx,ny,nz]
//   [vertexCount x uint8 r,g,b]        (present only when a color source is given)
//   [indexCount x uint32]
//   little-endian, world-space.
//
// Each ARMeshAnchor's geometry is in the anchor's local space; vertices are transformed
// to world by anchor.transform and normals by its rotation. Anchors are concatenated
// with index offsets so the whole room is one mesh. When `colorAt` is supplied, each
// vertex gets a camera-sampled RGB (from the fused LiDAR cloud) so the viewer can shade
// the polygons instead of drawing a flat wireframe.

import ARKit
import simd

enum RoomMeshEncoder {
    static func encode(_ anchors: [ARMeshAnchor],
                       colorAt: ((SIMD3<Float>) -> SIMD3<UInt8>?)? = nil) -> Data? {
        var positions: [SIMD3<Float>] = []
        var normals: [SIMD3<Float>] = []
        var colors: [SIMD3<UInt8>] = []
        var indices: [UInt32] = []
        let defaultColor = SIMD3<UInt8>(140, 140, 150)  // for vertices not yet observed

        for anchor in anchors {
            let g = anchor.geometry
            let vCount = g.vertices.count
            guard vCount > 0 else { continue }
            let base = UInt32(positions.count)
            let transform = anchor.transform
            let rotation = simd_float3x3(
                SIMD3(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
                SIMD3(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
                SIMD3(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
            )

            // Vertices (ARGeometrySource, float3).
            let vBuf = g.vertices.buffer.contents()
            let vStride = g.vertices.stride
            let vOffset = g.vertices.offset
            // Normals (ARGeometrySource, float3), if present.
            let nSource = g.normals
            let nBuf = nSource.buffer.contents()
            let nStride = nSource.stride
            let nOffset = nSource.offset

            for i in 0..<vCount {
                let vp = vBuf.advanced(by: vOffset + vStride * i).assumingMemoryBound(to: SIMD3<Float>.self).pointee
                let world = transform * SIMD4<Float>(vp, 1)
                positions.append(SIMD3(world.x, world.y, world.z))
                let np = nBuf.advanced(by: nOffset + nStride * i).assumingMemoryBound(to: SIMD3<Float>.self).pointee
                normals.append(simd_normalize(rotation * np))
                if colorAt != nil {
                    let w = SIMD3<Float>(world.x, world.y, world.z)
                    colors.append(colorAt?(w).flatMap { $0 } ?? defaultColor)
                }
            }

            // Faces (ARGeometryElement): triangles, bytesPerIndex is 4 (UInt32) on device.
            let faces = g.faces
            let indexCount = faces.count * faces.indexCountPerPrimitive
            let fBuf = faces.buffer.contents()
            if faces.bytesPerIndex == 4 {
                let p = fBuf.assumingMemoryBound(to: UInt32.self)
                for i in 0..<indexCount { indices.append(base + p[i]) }
            } else {
                let p = fBuf.assumingMemoryBound(to: UInt16.self)
                for i in 0..<indexCount { indices.append(base + UInt32(p[i])) }
            }
        }

        guard !positions.isEmpty, !indices.isEmpty else { return nil }

        var data = Data(capacity: 8 + positions.count * 27 + indices.count * 4)
        var vCount = UInt32(positions.count).littleEndian
        var iCount = UInt32(indices.count).littleEndian
        withUnsafeBytes(of: &vCount) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &iCount) { data.append(contentsOf: $0) }
        // Write x,y,z as three separate floats: SIMD3<Float> is 16-byte padded, so
        // dumping its raw bytes would emit 4 stray bytes per vertex and misalign the
        // reader (the room.bin format is tightly packed, 12 bytes/vertex).
        for p in positions {
            var x = p.x, y = p.y, z = p.z
            withUnsafeBytes(of: &x) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: &y) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: &z) { data.append(contentsOf: $0) }
        }
        for n in normals {
            var x = n.x, y = n.y, z = n.z
            withUnsafeBytes(of: &x) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: &y) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: &z) { data.append(contentsOf: $0) }
        }
        for c in colors { data.append(c.x); data.append(c.y); data.append(c.z) }
        for i in indices { var v = i.littleEndian; withUnsafeBytes(of: &v) { data.append(contentsOf: $0) } }
        return data
    }
}
