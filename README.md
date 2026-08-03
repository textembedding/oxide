# Swarm Harness

This is a disposable native macOS launcher for Codex workers implementing the
memory roadmap. It exists to dogfood the future Rust journal kernel's complete
worker interface:

- `journal_search`
- `journal_add`

The current backend is a small SQLite prototype in
`src/swarm_harness/journal.py`. No other module imports SQLite or performs a
task-state transition. Workers select, claim, implement, verify, integrate, and
complete their own tasks through those two operations.

## Run Stage 0

```bash
./swarmctl harness run \
  --workload stage0 \
  --target /Users/cat/Documents/code/memory \
  --workers 7
```

`stage0` is the workload name. `pilot` is no longer used. A run opens one thin
launcher terminal and seven worker terminals. Each worker receives an
independent clone and a stable slot; 15 of the 16 Stage 0 tasks are immediately
available, so all seven workers can claim work.

## Observe

```bash
# Worker Codex JSONL, including visible journal_search/journal_add calls.
./swarmctl harness observe --workload stage0 --slot worker-0

# Thin launcher activity.
./swarmctl harness observe --workload stage0 --slot orchestrator

# Compact single-column queue; blocked tasks are omitted.
./swarmctl harness observe-queue --workload stage0

# One JSON snapshot.
./swarmctl harness status --workload stage0
```

The stream observer preserves the original terminal syntax coloring for model
messages, reasoning summaries, commands, diffs, and journal YAML. Control
characters are escaped and continuation lines remain indented beneath their
timestamp.

## Pause, resume, and reset

```bash
./swarmctl harness pause --workload stage0
./swarmctl harness resume --workload stage0

# Stop, archive the complete run directory, and delete only its integration ref.
./swarmctl harness reset --workload stage0
```

Pause terminates launcher, worker, and Codex processes after recording
`control: pause`. Claims and worker clones remain. Resume records
`control: resume`, relaunches the same stable slots, and each worker recovers by
searching its journal records and continuing in its existing clone.

Reset archives the run under `.swarm/archive/`; it does not delete the target
repository or its current branch. Run the Stage 0 command again after reset to
start from scratch.

See `HARNESS_CONTRACT.md` for the complete disposable protocol and
`ACCEPTANCE.md` for the model-free proof suite.
