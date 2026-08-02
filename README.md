# Swarm Harness

This repository coordinates native macOS Codex workers implementing Stages
0–3 while persisting task state in a disposable SQLite journal.

Start with:

1. `HARNESS_CONTRACT.md`
2. `NON_GOALS.md`
3. `ACCEPTANCE.md`
4. `prompts/01-journal.md`

Use `./swarmctl harness run` to launch an orchestrator and visible worker
terminals, and `./swarmctl harness observe` to follow any slot.
