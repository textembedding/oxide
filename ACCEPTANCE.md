# Acceptance Criteria

The harness prototype is viable when all of the following are demonstrated:

1. Two concurrent workers cannot both claim the same task.
2. A worker cannot submit with an incorrect, stale, or superseded claim token.
3. A valid submission and its open proposal persist across journal and launcher restart.
4. A vanished local worker is observed, fenced, and replaced immediately; an
   optional explicit lease still expires for an unobservable worker.
5. Dependencies prevent downstream tasks from becoming runnable too early.
6. A proposal author cannot vote, each validator votes once, and two matching
   independent votes are required to commit.
7. Candidate checks run in independent workers; the launcher can merge only an
   already committed acceptance proposal.
8. Retry, decomposition, dependency change, and stage completion transitions
   are journal quorum commits rather than launcher decisions.
9. A three-task toy stage completes end to end through proposal quorum.
10. The toy stage later runs against the Rust backend without harness changes.
11. One real Stage 0 task completes through the same claim, submit, verify,
    and merge path.

Passing these criteria completes the Python prototype. It then freezes except
for defects that violate this contract. Production behavior belongs in Rust.
