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

## Review UI Layout Notes

- Preferred review page header hierarchy:
  - top row: app title `review_app` on the left
  - top row right side: `Scopes` and `Summary` controls
  - second row: full-width scope/queue status bar
- The scope/queue status bar should contain:
  - `Scope`
  - `Queue Position`
  - `Queue Depth`
- Candidate metadata should not dominate the initial review panel.
- The detailed candidate info block should be collapsible.
- In collapsed state, the candidate info block should show only:
  - source path
  - burst progress in compact form like `36/47`
- The expanded state can continue to show the fuller metadata set.
- Recent label buttons must always display visible species names, not just shortcuts.
- Outcome buttons should display their keyboard shortcuts directly in the button text when a shortcut exists.
- The eBird / Macaulay reference panel should sit near the classifier suggestion, before the main labeling controls.
