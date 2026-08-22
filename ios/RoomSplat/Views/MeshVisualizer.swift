import ARKit
import SceneKit

/// Renders ARKit LiDAR scene-reconstruction mesh anchors as a wireframe overlay in an
/// ARSCNView — the "mini 3D model" the AR SDK builds while LiDAR is scanning. Nodes are
/// kept in sync with the anchors; SceneKit mutations are hopped to the main thread.
final class MeshVisualizer {
    private weak var scnView: ARSCNView?
    private var nodes: [UUID: SCNNode] = [:]

    var isHidden = false {
        didSet { DispatchQueue.main.async { self.nodes.values.forEach { $0.isHidden = self.isHidden } } }
    }

    init(scnView: ARSCNView) { self.scnView = scnView }

    func update(_ anchor: ARMeshAnchor) {
        guard let geometry = Self.wireframeGeometry(from: anchor.geometry) else { return }
        let transform = anchor.transform
        let id = anchor.identifier
        DispatchQueue.main.async {
            let node: SCNNode
            if let existing = self.nodes[id] {
                node = existing
            } else {
                node = SCNNode()
                self.nodes[id] = node
                self.scnView?.scene.rootNode.addChildNode(node)
            }
            node.geometry = geometry
            node.simdTransform = transform
            node.isHidden = self.isHidden
        }
    }

    func remove(_ id: UUID) {
        DispatchQueue.main.async {
            self.nodes[id]?.removeFromParentNode()
            self.nodes[id] = nil
        }
    }

    func clear() {
        DispatchQueue.main.async {
            self.nodes.values.forEach { $0.removeFromParentNode() }
            self.nodes.removeAll()
        }
    }

    private static func wireframeGeometry(from mesh: ARMeshGeometry) -> SCNGeometry? {
        let vertices = mesh.vertices
        let source = SCNGeometrySource(
            buffer: vertices.buffer,
            vertexFormat: vertices.format,
            semantic: .vertex,
            vertexCount: vertices.count,
            dataOffset: vertices.offset,
            dataStride: vertices.stride
        )

        let faces = mesh.faces
        let faceData = Data(bytes: faces.buffer.contents(), count: faces.buffer.length)
        let element = SCNGeometryElement(
            data: faceData,
            primitiveType: .triangles,
            primitiveCount: faces.count,
            bytesPerIndex: faces.bytesPerIndex
        )

        let geometry = SCNGeometry(sources: [source], elements: [element])
        let material = SCNMaterial()
        material.fillMode = .lines
        material.diffuse.contents = UIColor.systemGreen
        material.isDoubleSided = true
        material.lightingModel = .constant
        geometry.materials = [material]
        return geometry
    }
}
