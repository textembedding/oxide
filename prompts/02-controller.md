# Thin Launcher with Fake Workers

Implement only process liveness observation, worktree preparation, application
of journal-committed merges, and a toy-stage runner using fake workers.

## Allowed files

- `src/swarm_harness/controller.py`
- `tests/test_controller.py`
- `stages/toy.yaml`

## Hard limits

- At most 800 non-test source lines added in this milestone.
- No source file longer than 500 lines.
- No real Codex processes.
- No new worker-visible tools.
- No acceptance checks, retry decisions, graph decisions, or terminal stage
  decisions in the launcher.

Stop after the controller tests pass.
