# RoomSplat iOS

iOS 18+, Swift 6.1, physical device required — the Simulator cannot validate ARKit
capture, LiDAR, thermal state, or on-device training (SPEC.md §7). This directory is a
source scaffold; it is not yet a buildable Xcode project (no `.xcodeproj` here — add
one and add these files to its target).

## Implemented (spec-critical, reviewed against SPEC.md)

- `Capture/CoordinateTransform.swift` — ARKit → Nerfstudio transform, **ported
  verbatim** from the reference writer (§5). See `THIRD_PARTY_NOTICES.md` in that
  directory; the reference is PolyForm Noncommercial 1.0.0 (§0.6).
- `Capture/KeyframeSelector.swift` — M1 acceptance rules (15 cm / 10°, blur median,
  tracking `.normal`, 300 cap) plus M3 backpressure escalation.
- `Stream/IngestClient.swift` — single-WebSocket wire protocol, binary framing,
  reconnect/resume from highest ack (§3, §4).
- `Governor/ThermalGovernor.swift` — required thermal policy (§2).

## Not yet ported (needs Xcode + device)

- `Capture/` ARSession wiring, LiDAR fusion (2 cm voxel, 500k cap, planar bias),
  exposure/WB/focus locks and per-frame assertions, `.roomsplat` debug writer (M1).
- `Preview/` msplat coarse training + MetalSplatter PiP (M5).
- Coverage analysis overlay (M5).
- `Views/`, settings (server host, mDNS discovery, §7).

Fork these from `vendor/ios-gaussian-splatting-demo/3DGS Demo/` (Capture, Training,
Viewer) per SPEC.md M1/M5, preserving attribution.
