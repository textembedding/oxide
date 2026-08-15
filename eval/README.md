# Planning prompt evaluation

This directory evaluates the planning prompt without prescribing a gold
`ROADMAP.md`. A gold roadmap would reward one arbitrary decomposition and teach
the planner to imitate the label. Oxide instead scores observable planning
invariants and relations between paired inputs.

## Objectives

Layer 1 is deterministic correctness:

- the response and roadmap schemas are valid;
- every cited requirement resolves against exact source semantics;
- every rubric requirement receives a disposition;
- readiness and approval reflect whether the source is contractible;
- dependency ordering preserves stated prerequisites without fixing a stage count;
- each phase includes a meaningful formal-verification goal;
- fixture-specific unsupported concepts do not appear.

Layer 2 is metamorphic and adversarial behavior:

- Markdown-only formatting changes preserve the semantic plan;
- removing a necessary acceptance criterion makes affected work non-ready;
- contradictory requirements are surfaced instead of silently reconciled.

An optional LLM judge scores faithfulness, decomposition quality, and human
readability only after deterministic qualification succeeds. Judge feedback can
improve a prompt, but it cannot turn an invalid source trace into a passing result.

Each score includes compact diagnostics. [GEPA](https://github.com/gepa-ai/gepa) receives those diagnostics as
actionable side information and proposes a replacement Jinja template. Candidate
templates must preserve the production variable contract. The best result is
written under `eval/runs/` for review; it is never installed automatically.

## Smoke examples

The three deliberately small examples exercise the evaluation plumbing:

1. `durable-counter`: a formatting-equivalent specification pair;
2. `retry-queue`: a complete retry contract paired with a missing terminal rule;
3. `private-notes`: a coherent privacy contract paired with a direct contradiction.

Their rubrics identify source requirements, allowed readiness states, real
ordering constraints, and adversarial signals. They do not specify stage names,
stage counts, prose, or a preferred full roadmap. Fixture roadmaps exist only to
test the evaluation machinery without paying for model calls.

These examples validate the pipeline; they are not a sufficient optimization or
holdout set. Journal-scale benchmark examples should contain 1,500–3,000 lines of
specification spread across product, development, and research concerns. Never
promote a prompt based only on the smoke score.

## Benchmark examples

The serious suite adds three unrelated Rust products whose base specification
corpora contain 1,500–3,000 lines each:

1. [`transactional-reservation`](examples/transactional-reservation) — a
   **transactional reservation and settlement kernel** with bounded inventory,
   atomic holds, expiration, confirmation, cancellation, refunds, idempotent
   retries, multi-item failure rules, trusted payment and clock adapters, crash
   recovery, tenant isolation, and capacity targets. This tests concurrent state
   transitions, error precedence, trusted boundaries, and the distinction between
   formally proved safety and measured performance.
2. [`collaborative-document`](examples/collaborative-document) — an **offline
   collaborative document engine** with causal operations, convergence,
   conflict resolution, snapshots, reconnect synchronization, access control,
   schema evolution, malformed-peer isolation, and scale targets. This tests
   whether the planner finds independent layers without inventing conflict
   semantics or treating distributed-system experiments as proofs.
3. [`agent-message-board`](examples/agent-message-board) — a **globally
   consistent agent message board**, or "Slack for agents," designed
   for many-to-many communication between large numbers of concurrent Codex
   sessions. Messages are immutable, content-addressed records in one globally
   consistent chronology, with typed forms for discoveries, questions, claims,
   proposals, decisions, corrections, blockers, results, and handoffs. Agents can
   atomically publish to multiple topics, cite causal predecessors, claim work,
   subscribe by task or concept, maintain durable cursors, and recover an exact
   shared view after a crash. The service provides deterministic conflict
   resolution for competing claims and decisions, bounded context-recovery
   queries, deduplicated fan-out, backpressure, access capabilities, live shard
   movement, and checkpointing without pausing publication. Unlike Slack, it is
   optimized for machine attention rather than channel scrolling: one record can
   reach every interested agent without inbox copies, structured outcomes can be
   consumed mechanically, corrections preserve provenance, and a new session can
   reconstruct the decisions and unresolved work relevant to its assignment.
   This tests whether the planner can preserve global consistency independently
   of physical sharding, separate typed coordination semantics from retrieval and
   presentation, prove concurrency and recovery invariants, isolate trusted
   network and storage effects, and keep formal guarantees distinct from measured
   fan-out, throughput, latency, and retrieval quality.

Each base corpus contains no roadmap or stage labels. Its adversarial variant is a
small semantic patch over the complete base corpus rather than a duplicated copy.
The checked-in variants are public tuning examples; prompt promotion should also
depend on unseen holdout perturbations.

## Commands

Install the optional optimizer:

```bash
uv sync --extra dev --extra eval
```

Validate the scorer and real GEPA integration without model calls:

```bash
uv run python -m eval smoke
```

Score the current prompt with real Codex planning turns:

```bash
uv run python -m eval score --runner codex
```

Add `--judge codex` to include the soft quality objective. A score run performs
two planning turns per case; the judge adds one turn per case.

Run a bounded hill climb. Both planning evaluations and reflective mutations use
the signed-in Codex CLI, while GEPA supplies candidate selection and feedback:

```bash
uv run python -m eval optimize \
  --runner codex \
  --proposer codex \
  --max-metric-calls 30 \
  --max-proposals 8 \
  --output eval/runs/planning-v1
```

The output contains `planning.optimized.md.j2` and `gepa-result.json`. Review the
candidate and run it on unseen cases before replacing the production prompt.
