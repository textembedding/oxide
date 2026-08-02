# Repository Instructions

This is a clean-room implementation. Do not inspect, copy, import,
summarize, or request access to any legacy swarm-harness implementation.

## Frozen constraints

- The model-visible tools are exactly `claim_task` and `submit_result`.
- The Python journal is a disposable SQLite prototype.
- The Rust kernel later replaces it behind the same JSON protocol.
- Workers cannot add executable tasks.
- Task expansion occurs only during explicit controller planning phases.
- Each task runs in its own Git branch and worktree.
- Each worker runs in a separate visible terminal window.
- Do not use tmux.
- Do not build process-authority, sandbox-proof, verifier-broker,
  runtime-integrity, artifact-retirement, or recursive-replay subsystems.

## Hard limits

- At most 3,000 non-test Python source lines.
- At most 3,000 Python test lines.
- At most 500 lines in any source file.
- Only the source modules already present under `src/swarm_harness/`.
- Each milestone must remain within the line budget in its prompt.

When a requirement cannot fit these constraints, stop and report the
conflict. Do not expand scope.
