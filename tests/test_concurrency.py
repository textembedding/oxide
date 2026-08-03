from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_harness.concurrency import ROLES, ConcurrencyError, run_campaign, validate_receipt

ROOT = Path(__file__).parents[1]


def test_real_multiprocess_claim_crash_and_replay_campaign(tmp_path: Path) -> None:
    output = tmp_path / "validation"
    report = run_campaign(ROOT, output, workers=2, rounds=2, seed=912_771, log=lambda _: None)

    assert report["status"] == "passed"
    assert report["roles"] == list(ROLES)
    assert report["case_count"] == 10
    assert report["winner_crash_cases"] == 5
    assert set(report["invariants"].values()) == {True}
    assert {item["crash_after_win"] for item in report["cases"]} == {False, True}
    assert all(item["claim_records_appended"] == 2 for item in report["cases"])
    assert all(item["protected_records"] == 1 for item in report["cases"])
    assert all(len(item["workflow_digest"]) == 64 for item in report["cases"])
    assert all(len(item["journal_digest"]) == 64 for item in report["cases"])

    receipt = validate_receipt(
        ROOT,
        output / "latest.json",
        required_workers=2,
        minimum_rounds=2,
    )
    assert receipt["seed"] == 912_771
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
