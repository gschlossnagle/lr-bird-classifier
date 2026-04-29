# Project Notes

## Documentation Discipline

- Any user-facing change should include a documentation update in the same change.
- User-facing changes include:
  - CLI flags or command behavior
  - review UI behavior, workflows, or visible labels
  - extraction defaults or scope behavior
  - export behavior or output format
  - setup, install, or runtime expectations
- The documentation update does not need to be large, but it should keep the
  README and any relevant file in `docs/` aligned with the current behavior.
- If a change is only captured in code or tests, assume the documentation is
  incomplete and update it before considering the work finished.
