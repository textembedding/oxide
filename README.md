# Swarm Harness

This repository coordinates native macOS Codex workers implementing Stages
0–3 while persisting task state in a disposable SQLite journal.

Start with:

1. `HARNESS_CONTRACT.md`
2. `NON_GOALS.md`
3. `ACCEPTANCE.md`
4. `prompts/01-journal.md`

Use `./swarmctl harness run` to launch a thin local process launcher and visible
worker terminals, and `./swarmctl harness observe` to follow any slot. The
legacy observer slot name `orchestrator` is retained for command compatibility;
that process has no acceptance or completion authority.

Workers atomically claim routine implementation or validation work from the
journal. Candidate acceptance, retries, task/dependency changes, and stage
completion require two matching votes from up to three independent workers.
The proposal author cannot vote. The launcher only observes local process
liveness, prepares Git worktrees, and applies merges already committed by the
journal quorum.

Lifecycle controls:

```bash
# Stop workers safely and preserve accepted and in-progress task worktrees.
./swarmctl harness pause --workload stage0

# Continue the preserved workload with its original target and worker count.
./swarmctl harness resume --workload stage0

# Stop and archive the workload, then remove only its worktrees and branches.
./swarmctl harness reset --workload stage0

# Start from scratch after reset.
./swarmctl harness run \
  --workload stage0 \
  --target /Users/cat/Documents/code/memory \
  --workers 7
```

`reset` preserves the old journal and logs under `.swarm/archive/` for recovery
but removes the active run marker, so the next `run` is fresh.
