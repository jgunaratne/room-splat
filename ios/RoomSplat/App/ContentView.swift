import ARKit
import SwiftUI

struct ContentView: View {
    @StateObject private var coordinator = CaptureCoordinator()

    var body: some View {
        ZStack {
            if CaptureCoordinator.deviceSupported {
                CameraPreview(coordinator: coordinator)
                    .ignoresSafeArea()
            } else {
                Color.black.ignoresSafeArea()
            }

            VStack {
                statusBar
                Spacer()
                controls
            }
            .padding()
        }
    }

    private var statusBar: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(linkColor)
                .frame(width: 10, height: 10)
            Text(coordinator.status)
            Spacer()
            Text(thermalLabel)
        }
        .font(.footnote.weight(.medium))
        .foregroundStyle(.white)
        .padding(8)
        .background(.black.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
    }

    private var linkColor: Color {
        switch coordinator.link {
        case .offline: return .gray
        case .connecting: return .yellow
        case .connected: return .blue
        case .transmitting: return .green
        case .error: return .red
        }
    }

    private var controls: some View {
        VStack(spacing: 12) {
            HStack(spacing: 16) {
                stat("Keyframes", "\(coordinator.keyframeCount)")
                stat("Points", "\(coordinator.pointCount)")
            }

            TextField("server host, e.g. 192.168.1.50:8000", text: $coordinator.serverHost)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                .disabled(coordinator.isCapturing)

            Button(action: toggle) {
                Text(coordinator.isCapturing ? "Stop Capture" : "Start Capture")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .tint(coordinator.isCapturing ? .red : .accentColor)

            if !CaptureCoordinator.deviceSupported {
                Text("This device has no LiDAR. Capture needs an iPhone/iPad Pro.")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.8))
            }
        }
        .padding(12)
        .background(.black.opacity(0.5), in: RoundedRectangle(cornerRadius: 12))
    }

    private func stat(_ title: String, _ value: String) -> some View {
        VStack {
            Text(value).font(.title3.bold().monospacedDigit())
            Text(title).font(.caption2)
        }
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity)
    }

    private func toggle() {
        if coordinator.isCapturing {
            coordinator.stop()
        } else {
            coordinator.start()
        }
    }

    private var thermalLabel: String {
        switch coordinator.thermalState {
        case .nominal: return "thermal: nominal"
        case .fair: return "thermal: fair"
        case .serious: return "thermal: serious"
        case .critical: return "thermal: critical"
        @unknown default: return "thermal: —"
        }
    }
}

#Preview {
    ContentView()
}
