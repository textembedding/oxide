# Acceptance Criteria

The harness prototype is viable when all of the following are demonstrated:

1. Two concurrent workers cannot both claim the same task.
2. A worker cannot submit with an incorrect, stale, or superseded claim token.
3. Every Codex invocation exposes only `journal_add` and `journal_search` as
   journal tools, and both calls appear in the observable JSONL stream.
4. Submission is rejected unless the current attempt searched and persisted an
   exact checkpoint and handoff through those tools.
5. A valid submission and its open proposal persist across journal and launcher restart.
6. A vanished local worker is observed, fenced, and replaced immediately; an
   optional explicit lease still expires for an unobservable worker.
7. Dependencies prevent downstream tasks from becoming runnable too early.
8. A proposal author cannot vote, each validator votes once, and two matching
   independent votes are required to commit.
9. Candidate checks run in independent workers; the launcher can merge only an
   already committed acceptance proposal.
10. Retry, decomposition, dependency change, and stage completion transitions
   are journal quorum commits rather than launcher decisions.
11. A three-task toy stage completes end to end through proposal quorum.
12. The toy stage later runs against the Rust backend without harness changes.
13. One real Stage 0 task completes through the same claim, submit, verify,
    and merge path.

Passing these criteria completes the Python prototype. It then freezes except
for defects that violate this contract. Production behavior belongs in Rust.
