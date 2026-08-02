"""The complete harness-specific worker tool surface."""

from __future__ import annotations

from typing import Any

from .journal_client import JournalClient

__all__ = ["claim_task", "submit_result"]


def claim_task(
    client: JournalClient,
    *,
    run_id: str,
    worker_id: str,
    lease_seconds: float,
) -> dict[str, Any]:
    return client.claim_task(run_id, worker_id, lease_seconds)


def submit_result(
    client: JournalClient,
    *,
    run_id: str,
    task_id: str,
    claim_token: str,
    outcome: str,
    summary: str,
    commit_sha: str,
    blockers: list[object] | None = None,
    proposed_followups: list[object] | None = None,
) -> dict[str, Any]:
    return client.submit_result(
        run_id=run_id,
        task_id=task_id,
        claim_token=claim_token,
        outcome=outcome,
        summary=summary,
        commit_sha=commit_sha,
        blockers=list(blockers or []),
        proposed_followups=list(proposed_followups or []),
    )
