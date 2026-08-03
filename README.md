# Swarm Harness

This is a disposable native macOS launcher for Codex workers implementing the
memory roadmap. It dogfoods the future Rust journal kernel's complete worker
interface:

- `journal_search`
- `journal_add`

The current kernel prototype is the generic append-only SQLite store in
`src/swarm_harness/journal.py`. It knows only namespaces, authors, immutable
text records, and generic text search. All task, PR, review, generation,
dependency, and merge behavior is implemented above it by the replayable
reducer in `src/swarm_harness/workflow.py`. Replacing the prototype kernel does
not require porting any workflow policy.

## Run Stage 0

```bash
./swarmctl harness run \
  --workload stage0 \
  --target /Users/cat/Documents/code/memory \
  --workers 7 \
  --reviews 3
```

`stage0` is the workload name; `pilot` is no longer used. Three internal
approvals per task PR is the default, so `--reviews 3` may be omitted. A run
opens one thin launcher terminal and seven worker terminals. Each Codex session
may author, revise, review, or authorize a merge according to the ready work it
claims from the journal.

Every task uses its own branch under the run's `codex/swarm-*` prefix. There is
no shared integration branch. After author self-verification, three distinct
workers review the exact candidate head. A worker then authorizes its merge;
the launcher re-runs the task checks on the prospective merge tree and merges
that exact tree directly into the target branch. Dependencies are released
only after that merge. `COMPLETE` therefore means all reviewed task changes are
present in the target checkout and the 76-command Stage 0 gate passed there.

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

# Stop, archive the run directory, and delete its task PR branches.
./swarmctl harness reset --workload stage0
```

Pause records `control: pause` and terminates launcher, worker, and Codex
processes. Claims, records, and worker clones remain. Resume records
`control: resume`, relaunches the stable slots, and each replacement reconstructs
its work from `journal_search`.

Reset archives the run under `.swarm/archive/` and deletes only branches under
that run's task-branch prefix. It does not delete the target repository or
revert changes already merged into its current branch. Run the Stage 0 command
again after reset to start from the branch's current commit.

See `HARNESS_CONTRACT.md` for the disposable workflow and `ACCEPTANCE.md` for
the model-free proof suite.
