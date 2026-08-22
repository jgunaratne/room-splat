"""RoomSplat server package.

Shared building blocks for the desktop side of the pipeline: the .roomsplat data
contract (see SPEC.md §6), the live-ingest disk mirror, spatial chunking, and the
viewer manifest. Kept import-light so tests run without a GPU or torch.
"""

SCHEMA_VERSION = 2
