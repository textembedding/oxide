# Milestone 3: Real Worker Adapter

Implement only the real implementation/validation worker, two-tool adapter,
Git branch/worktree flow, process-group cancellation, and visible-terminal
launch. Validation claims run exact candidate checks and submit evidence-bound
votes; authors never validate their own proposal.

## Allowed files

- `src/swarm_harness/worker.py`
- `src/swarm_harness/tools.py`
- `tests/test_worker.py`

## Hard limits

- At most 800 non-test source lines added in this milestone.
- No source file longer than 500 lines.
- Exactly two worker-visible tools.
- No tmux.
- No process-authority, sandbox-proof, verifier-broker, or trace-capture system.

Stop after one real toy task succeeds end to end.
