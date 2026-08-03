# Memory Roadmap Workloads

Each file in `stages/` is an executable work contract consumed by the workflow
layer. The generic journal kernel never parses it. A task contains an ID,
objective, dependencies, and exact checks.

`stages/stage0.yaml` is derived from the memory specification and contains the
complete 16-milestone Stage 0 implementation graph plus its 76-command closing
gate. Fifteen milestones are independent and immediately claimable; the final
decision-verifier milestone depends on all fifteen.

`smoke.yaml` is the three-task model-free end-to-end fixture used by
`./swarmctl verify`.

Later roadmap contracts can be added as `stages/<workload>.yaml` and will run
through the same two-tool workers and per-task review workflow. Empty stage
placeholders and harness lifecycle detail do not belong in this directory.
