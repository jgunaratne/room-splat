"""NanoGS post-training simplification (SPEC.md M6).

Training-free: load the trained PLY, set a target ratio and merge cap, get a smaller
PLY back, then convert to SPZ for final delivery (target < 30 MB). Uses the Python
implementation server-side (`saliteta/NanoGS`) — the browser KD-tree is unreliable
above ~5M splats and there is no reason to do this in the browser.

This is a thin invocation wrapper; the actual reduction runs on the 5090 box where
NanoGS and its deps are installed (not part of the GPU-free default install).
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("roomsplat.simplify")


def simplify(input_ply: Path, output_ply: Path, target_ratio: float = 0.5, merge_cap: int = 8) -> Path:
    try:
        import nanogs  # type: ignore  # noqa: F401
    except ImportError as e:  # pragma: no cover - depends on the box
        raise RuntimeError(
            "NanoGS is not installed. Install saliteta/NanoGS on the 5090 box (SPEC.md M6). "
            "It is intentionally not a default dependency."
        ) from e
    # nanogs API: load -> simplify(target_ratio, merge_cap) -> save
    raise NotImplementedError(
        "Wire this to the installed NanoGS entrypoint on the GPU box and record the "
        "before/after splat count and wall-clock in the stage log (SPEC.md §8)."
    )
