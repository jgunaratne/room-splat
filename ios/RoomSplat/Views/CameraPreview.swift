import ARKit
import SceneKit
import SwiftUI

/// Live ARKit camera feed. The coordinator drives the shared ARSession; ARSCNView
/// just renders its camera background.
struct CameraPreview: UIViewRepresentable {
    let coordinator: CaptureCoordinator

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.automaticallyUpdatesLighting = true
        coordinator.attach(view)
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}
