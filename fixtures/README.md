# fixtures/

Keep at least three recorded `.roomsplat` sessions here and run them through
`--replay` in CI (SPEC.md §8). Every performance claim in this project is measured
against identical recorded input via replay, never against a fresh walkthrough.

These are real device captures (M1 debug mode or M3 disk mirrors), so they are added
once a physical iPhone is available. They are intentionally not committed as binaries
in this scaffold; drop them in as `fixtures/<name>.roomsplat/` and reference them from
the CI replay job.
