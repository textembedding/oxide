from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from oxide.evidence import (
    begin_attempt,
    evidence_key,
    finish_attempt,
    load_terminal_receipt,
)
from oxide.journal_backend import JournalError
from oxide.worker import Worker


class SearchOnlyClient:
    socket_path = "/tmp/journal.sock"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, run_id: str, query: str) -> list[dict]:
        assert run_id == "run"
        self.queries.append(query)
        if query == "run:state":
            return [{"state": "running"}]
        if query == "worker:worker-0":
            return [{"task_id": "A", "state": "working"}]
        if query == "queue:ready":
            return []
        raise AssertionError(query)

    def worker_snapshot(self, run_id: str, worker: str) -> tuple[str, list[dict], list[dict]]:
        return (
            self.search(run_id, "run:state")[0]["state"],
            self.search(run_id, f"worker:{worker}"),
            self.search(run_id, "queue:ready"),
        )


class ClaimClient(SearchOnlyClient):
    def __init__(self) -> None:
        super().__init__()
        self.claims: list[str] = []

    def search(self, run_id: str, query: str) -> list[dict]:
        if query == "worker:worker-1":
            return []
        if query == "queue:ready":
            return [{"claim": f"claim: task:{task}"} for task in ("A", "B", "C")]
        return super().search(run_id, query)

    def add(self, run_id: str, worker: str, text: str) -> dict:
        assert (run_id, worker) == ("run", "worker-1")
        self.claims.append(text)
        work = next(item for item in self.search(run_id, "queue:ready") if item["claim"] == text)
        return {"claim": "accepted", "work": work}


class PriorityClient(ClaimClient):
    def search(self, run_id: str, query: str) -> list[dict]:
        if query == "worker:worker-1":
            return []
        if query == "queue:ready":
            return [
                {"claim": "claim: task:B", "role": "author"},
                {"claim": "claim: verify:A:" + "1" * 40 + ":1", "role": "verification"},
            ]
        return super().search(run_id, query)


def test_worker_host_uses_only_search_and_codex_gets_exact_two_tools(
    monkeypatch, tmp_path: Path
) -> None:
    capture = tmp_path / "capture.json"
    fake = tmp_path / "fake_codex.py"
    fake.write_text(
        """import json, os, sys
from pathlib import Path
Path(os.environ['CAPTURE']).write_text(json.dumps({'argv': sys.argv[1:], 'env': {key: value for key, value in os.environ.items() if key.startswith('OXIDE_')}}))
print(json.dumps({'type':'turn.completed'}))
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CAPTURE", str(capture))
    client = SearchOnlyClient()
    logs: list[str] = []
    worker = Worker(
        client,  # type: ignore[arg-type]
        "run",
        "worker-0",
        tmp_path,
        "main",
        tmp_path,
        journal_socket=tmp_path / "journal.sock",
        workflow_socket=tmp_path / "workflow.sock",
        codex_argv=(sys.executable, str(fake)),
        log=logs.append,
    )
    assert worker.run_once() == "worked"
    record = json.loads(capture.read_text(encoding="utf-8"))
    joined = " ".join(record["argv"])
    assert 'enabled_tools=["journal_add","journal_search"]' in joined
    assert "mcp_servers.journal.tool_timeout_sec=180" in joined
    assert "sandbox_workspace_write.network_access=true" in joined
    assert "journal_mcp" in joined
    assert str((tmp_path / ".git").resolve()) in joined
    assert str((Path.home() / ".docker" / "run").resolve()) in joined
    assert set(record["env"]) == {
        "OXIDE_JOURNAL_SOCKET",
        "OXIDE_RUN_CONFIG",
        "OXIDE_RUN_EPOCH",
        "OXIDE_RUN_ID",
        "OXIDE_WORKER_ID",
        "OXIDE_WORKFLOW_SOCKET",
    }
    prompt = record["argv"][-1]
    assert "queue:ready" in prompt
    assert "rotate the complete ready list left by 0" in prompt
    assert "host normally preclaims" in prompt
    assert "current or prior authorship does not exclude a slot" in prompt
    assert "INTERNAL REVIEW" in prompt
    assert "Acceptance-check assignments are executed directly" in prompt
    assert "one response is not necessarily exhaustive" in prompt
    assert "match_kind and stored routing metadata" in prompt
    assert "iterative follow-up searches" in prompt
    assert "configured approval count" in prompt
    assert "There is no integration branch" in prompt
    assert "lease" not in prompt.lower()
    assert client.queries == ["run:state", "worker:worker-0", "queue:ready"]
    assert any("turn.completed" in line for line in logs)


def test_worker_claims_rotated_ready_work_before_model_launch(monkeypatch, tmp_path: Path) -> None:
    client = ClaimClient()
    worker = Worker(
        client,
        "run",
        "worker-1",
        tmp_path,
        "main",
        tmp_path,
        journal_socket=tmp_path / "journal.sock",
    )  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "_codex", lambda: 0)
    assert worker.run_once() == "worked"
    assert client.claims == ["claim: task:B"]


def test_worker_claim_rotation_does_not_starve_acceptance_checks(
    monkeypatch, tmp_path: Path
) -> None:
    client = PriorityClient()
    worker = Worker(
        client,
        "run",
        "worker-1",
        tmp_path,
        "main",
        tmp_path,
        journal_socket=tmp_path / "journal.sock",
    )  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "_codex", lambda: 0)
    monkeypatch.setattr(worker, "_run_acceptance_check", lambda _item: 0)
    assert worker.run_once() == "worked"
    assert client.claims == ["claim: verify:A:" + "1" * 40 + ":1"]


class VerificationResultClient:
    def __init__(self, transient_failure: bool = False) -> None:
        self.transient_failure = transient_failure
        self.records: list[str] = []

    def add(self, run_id: str, worker: str, text: str) -> dict:
        assert (run_id, worker) == ("run", "worker-0")
        if self.transient_failure:
            self.transient_failure = False
            raise JournalError("temporary transport failure")
        self.records.append(text)
        return {"saved": True}


def _git_repository(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "product.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def test_acceptance_check_runs_once_and_retries_only_terminal_publication(tmp_path: Path) -> None:
    target = tmp_path / "target"
    head = _git_repository(target)
    executions = tmp_path / "executions"
    command = f'printf x >> {executions} && test "$(cat product.txt)" = ready'
    claim = f"claim: verify:A:{head}:1"
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=target,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    check = {
        "id": "acceptance",
        "driver": "command",
        "command": command,
        "working_directory": ".",
        "environment": {},
        "evidence_slot": "primary",
        "artifacts": [],
    }
    requirement = {
        "schema": "OxideCheckRequirementV1",
        "candidate": {"base": head, "commit": head, "tree": tree},
        "check": check,
        "qualification": {
            "environment": None,
            "timeout_seconds": 60,
            "max_artifact_bytes": 1024 * 1024,
            "infrastructure_exit_codes": [2, 124],
        },
    }
    item = {
        "claim": claim,
        "checks": [command],
        "check_contract": check,
        "head_sha": head,
        "tree_sha": tree,
        "role": "verification",
        "root_task_id": "A",
        "verification_ordinal": 1,
        "evidence_requirement": requirement,
        "evidence_key": evidence_key(requirement),
        "execution_attempt": "a" * 64,
    }
    client = VerificationResultClient(transient_failure=True)
    worker = Worker(
        client,  # type: ignore[arg-type]
        "run",
        "worker-0",
        target,
        "main",
        target,
        journal_socket=tmp_path / "journal.sock",
        evidence_root=tmp_path / "evidence" / "checks",
        log=lambda _line: None,
    )

    assert worker._run_acceptance_check(item) == 2
    assert executions.read_text(encoding="utf-8") == "x"
    assert worker._run_acceptance_check(item) == 0
    assert executions.read_text(encoding="utf-8") == "x"
    assert len(client.records) == 1
    assert client.records[0].startswith(f"verify-pass: {claim.removeprefix('claim: ')}\n")
    assert f"tree: {tree}" in client.records[0]
    assert "receipt: sha256:" in client.records[0]


def test_required_machine_receipt_fails_closed_and_valid_receipt_is_preserved(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    head = _git_repository(target)
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=target,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    def run(command: str, name: str) -> tuple[VerificationResultClient, Path]:
        claim = f"claim: verify:A:{head}:1"
        check = {
            "id": "proof",
            "driver": "command",
            "command": command,
            "working_directory": ".",
            "environment": {},
            "evidence_slot": "primary",
            "artifacts": [],
            "receipt_required": True,
        }
        requirement = {
            "schema": "OxideCheckRequirementV1",
            "candidate": {"base": head, "commit": head, "tree": tree},
            "check": check,
            "qualification": {
                "environment": None,
                "timeout_seconds": 60,
                "max_artifact_bytes": 1024 * 1024,
                "infrastructure_exit_codes": [2, 124],
            },
        }
        item = {
            "claim": claim,
            "checks": [command],
            "check_contract": check,
            "head_sha": head,
            "tree_sha": tree,
            "role": "verification",
            "root_task_id": "A",
            "verification_ordinal": 1,
            "evidence_requirement": requirement,
            "evidence_key": evidence_key(requirement),
            "execution_attempt": ("a" if name == "missing" else "b") * 64,
        }
        client = VerificationResultClient()
        evidence_root = tmp_path / name / "checks"
        worker = Worker(
            client,  # type: ignore[arg-type]
            "run",
            "worker-0",
            target,
            "main",
            target,
            journal_socket=tmp_path / "journal.sock",
            evidence_root=evidence_root,
            log=lambda _line: None,
        )
        assert worker._run_acceptance_check(item) == 0
        return client, evidence_root

    missing, _ = run("true", "missing")
    assert missing.records[0].startswith(f"verify-infrastructure: verify:A:{head}:1\n")

    valid, evidence_root = run(
        'printf \'{"schema":"ProductProofReceiptV1"}\\n\' > "$OXIDE_EVIDENCE_RECEIPT"',
        "valid",
    )
    assert valid.records[0].startswith(f"verify-pass: verify:A:{head}:1\n")
    receipt_files = list((evidence_root / "artifacts").glob("*.declared-receipt-json"))
    assert len(receipt_files) == 1
    assert json.loads(receipt_files[0].read_text(encoding="utf-8")) == {
        "schema": "ProductProofReceiptV1"
    }


def test_persisted_receipt_is_reusable_only_by_its_exact_execution_attempt(
    tmp_path: Path,
) -> None:
    requirement = {"schema": "OxideCheckRequirementV1", "candidate": "exact"}
    root = tmp_path / "evidence"
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdout.write_text("passed\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    begin_attempt(root, requirement, "a" * 64)
    finish_attempt(
        root,
        requirement,
        "a" * 64,
        result="passed",
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        maximum_artifact_bytes=1024,
        started_at=1.0,
    )

    assert (
        load_terminal_receipt(
            root,
            requirement,
            execution_attempt="a" * 64,
        )
        is not None
    )
    assert (
        load_terminal_receipt(
            root,
            requirement,
            execution_attempt="b" * 64,
        )
        is None
    )


def test_worker_detects_accepted_terminal_journal_record() -> None:
    event = {
        "item": {
            "type": "mcp_tool_call",
            "server": "journal",
            "tool": "journal_add",
            "status": "completed",
            "arguments": {"yaml": "text: 'approve: review:A:1:1'"},
            "result": {"content": [{"text": "saved: true"}]},
        }
    }
    assert Worker._terminal_record(json.dumps(event))
    event["item"]["arguments"] = {"yaml": "text: 'checkpoint: task:A'"}
    assert not Worker._terminal_record(json.dumps(event))
    event["item"]["arguments"] = {"yaml": "text: 'verify-pass: verify:A:" + "1" * 40 + ":1'"}
    assert Worker._terminal_record(json.dumps(event))
    event["item"]["arguments"] = {"yaml": "text: 'blocked: task:A'"}
    assert Worker._terminal_record(json.dumps(event))


def test_worker_persists_patch_in_completed_file_change_log(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    source = tmp_path / "example.py"
    source.write_text("before = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "example.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    source.write_text("after = True\n", encoding="utf-8")
    worker = Worker(
        object(),
        "run",
        "worker-0",
        tmp_path,
        "main",
        tmp_path,
        journal_socket=tmp_path / "journal.sock",
    )  # type: ignore[arg-type]
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "change-1",
                "type": "file_change",
                "changes": [{"path": str(source), "kind": "update"}],
                "status": "completed",
            },
        }
    )

    enriched = json.loads(worker._enrich_log_line(line))

    assert enriched["item"]["relative_paths"] == ["example.py"]
    assert "-before = True" in enriched["item"]["patch"]
    assert "+after = True" in enriched["item"]["patch"]


def test_worker_prompt_allows_task_tools_but_only_two_journal_operations(tmp_path: Path) -> None:
    worker = Worker(
        object(),
        "run",
        "worker-1",
        tmp_path,
        "main",
        tmp_path,
        journal_socket=tmp_path / "journal.sock",
    )  # type: ignore[arg-type]
    prompt = worker._prompt()
    assert "journal_search and journal_add are the only journal operations" in prompt
    assert "Use repository, Git, shell, and test tools normally" in prompt
    assert "blocked: task:<root_task_id>" in prompt
    assert "do not run the returned acceptance list" in prompt
    assert "do not mechanically rerun the declared command list" in prompt
    assert "executed directly by the qualified harness process" in prompt
    assert "`specification` asks whether the product model" in prompt
    assert "`adversarial` asks whether the production implementation" in prompt
    assert "`integration` asks whether concurrency, persistence, recovery" in prompt
    assert "Passing review cannot replace a required check result" in prompt
