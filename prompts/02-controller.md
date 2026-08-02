# Milestone 2: Controller with Fake Workers

Implement only dependency scheduling, submission verification, explicit
planning state, and a toy-stage runner using fake workers.

## Allowed files

- `src/swarm_harness/controller.py`
- `tests/test_controller.py`
- `stages/toy.yaml`

## Hard limits

- At most 800 non-test source lines added in this milestone.
- No source file longer than 500 lines.
- No real Codex processes.
- No new worker-visible tools.
- No task creation outside an explicit planning phase.

Stop after the controller tests pass.
