from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from oxide.concurrency import (
    ROLES,
    ConcurrencyError,
    implementation_digest,
    kernel_digest,
    run_campaign,
    validate_receipt,
)

ROOT = Path(__file__).parents[1]


def test_implementation_digest_covers_nested_engine_and_schema_files(tmp_path: Path) -> None:
    package = tmp_path / "src" / "oxide" / "verification"
    package.mkdir(parents=True)
    (package / "engine.py").write_text("VERSION = 1\n", encoding="utf-8")
    (package / "evidence.schema.json").write_text("{}\n", encoding="utf-8")
    for name in ("oxide", "pyproject.toml", "uv.lock"):
        (tmp_path / name).write_text(name + "\n", encoding="utf-8")
    original = implementation_digest(tmp_path)
    (package / "evidence.schema.json").write_text('{"schema": 1}\n', encoding="utf-8")
    assert implementation_digest(tmp_path) != original


def test_kernel_digest_binds_interpreter_script_bytes(tmp_path: Path) -> None:
    kernel = tmp_path / "kernel.py"
    kernel.write_text("version = 1\n", encoding="utf-8")
    first = kernel_digest([sys.executable, str(kernel)])
    kernel.write_text("version = 2\n", encoding="utf-8")
    assert kernel_digest([sys.executable, str(kernel)]) != first


def test_real_multiprocess_claim_crash_and_replay_campaign(tmp_path: Path) -> None:
    output = tmp_path / "validation"
    report = run_campaign(ROOT, output, workers=2, rounds=2, seed=912_771, log=lambda _: None)

    assert report["status"] == "passed"
    assert report["roles"] == list(ROLES)
    assert report["case_count"] == 10
    assert report["winner_crash_cases"] == 5
    assert (report["min_exact"], report["max_results"]) == (5, 10)
    assert report["replay_probe"]["record_count"] == 1_001
    assert set(report["invariants"].values()) == {True}
    assert {item["crash_after_win"] for item in report["cases"]} == {False, True}
    assert all(item["claim_records_appended"] == 2 for item in report["cases"])
    assert all(item["protected_records"] == 1 for item in report["cases"])
    assert all(item["protected_author"] == item["owner"] for item in report["cases"])
    assert all(len(item["claim_outcomes"]) == 2 for item in report["cases"])
    assert all(len(item["observed_by"]) == 2 for item in report["cases"])
    assert all(len(item["worker_replays"]) == 2 for item in report["cases"])
    assert all(
        len({replay["workflow_digest"] for replay in item["worker_replays"].values()}) == 1
        for item in report["cases"]
    )
    assert all(
        len({replay["journal_digest"] for replay in item["worker_replays"].values()}) == 1
        for item in report["cases"]
    )
    assert all(len(item["workflow_digest"]) == 64 for item in report["cases"])
    assert all(len(item["journal_digest"]) == 64 for item in report["cases"])

    receipt = validate_receipt(
        ROOT,
        output / "latest.json",
        required_workers=2,
        minimum_rounds=2,
    )
    assert receipt["seed"] == 912_771
    with pytest.raises(ConcurrencyError, match="stale"):
        validate_receipt(
            ROOT,
            output / "latest.json",
            required_workers=2,
            minimum_rounds=2,
            min_exact=1,
            max_results=10,
        )
    stale = json.loads((output / "latest.json").read_text(encoding="utf-8"))
    stale["source_digest"] = "0" * 64
    (output / "latest.json").write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ConcurrencyError, match="stale"):
        validate_receipt(
            ROOT,
            output / "latest.json",
            required_workers=2,
            minimum_rounds=2,
        )


def test_bound_receipt_survives_workspace_relocation(tmp_path: Path) -> None:
    original = tmp_path / "old" / ".oxide" / "validation"
    report = run_campaign(ROOT, original, workers=2, rounds=1, seed=442, log=lambda _: None)
    original_report = Path(report["report_path"])
    relocated_root = tmp_path / "new"
    relocated = (
        relocated_root
        / ".oxide"
        / "validation"
        / original_report.parent.name
        / original_report.name
    )
    relocated.parent.mkdir(parents=True)
    shutil.copy2(original_report, relocated)
    shutil.rmtree(tmp_path / "old")

    receipt = validate_receipt(
        relocated_root,
        original_report,
        required_workers=2,
        minimum_rounds=1,
        require_current_source=False,
    )
    assert receipt["receipt_digest"] == report["receipt_digest"]
