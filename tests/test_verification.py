from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from oxide.contract import ContractError, load_contract
from oxide.verification.driver import invocation
from oxide.verification.engine import (
    VerificationError,
    engine_digest,
    validate_policy,
)


def _contract() -> str:
    return """\
schema = 2
id = "formal-rust"
stage = "foundation"
enabled = true
minimum_reviews = 3
goal = "Implement a generically specified, pervasively verified Rust program."
hash_algorithm = "sha256"
manifest = "verification/manifest.toml"
toolchain_lock = "verification/toolchain.lock.toml"
verification_spec = "docs/VERIFICATION.md"
product_spec = "docs/PRODUCT.md"
immutable_paths = ["verification/contract.toml", "verification/toolchain.lock.toml", "docs"]
production_roots = ["src"]
contract_roots = ["verification/contracts"]
abstract_model_roots = ["verification/models"]
proof_roots = ["verification/proofs"]
trusted_adapter_roots = ["src/effects"]
non_authoritative_roots = ["verification/fixtures"]
production_features = ["production", "verified"]
target = "test-target"
production_entry = "src/lib.rs"
composition_root = "verification/proofs/composition.rs"
composition_module = "composition"
composition_theorem = "composition"
solver_rlimit = 10
additional_forbidden_patterns = []

[execution]
evidence_policy = "exact-verus-context-v1"
timeout_seconds = 60
infrastructure_exit_codes = [2, 124]
max_artifact_bytes = 1048576

[evidence]
max_log_bytes = 65536
max_artifacts = 16
max_artifact_bytes = 1048576

[[tasks]]
id = "COMPONENT"
title = "Implement one verified component"
prompt = "Implement executable Rust, meaningful contracts, and its refinement proof."
depends_on = []

[[tasks.checks]]
id = "component-proof"
driver = "verus"
operation = "proof"
root = "verification/proofs/component.rs"
"""


def _repository(root: Path) -> tuple[Path, Path]:
    repository = root / "repository"
    (repository / "verification" / "models").mkdir(parents=True)
    (repository / "docs").mkdir()
    (repository / "verification" / "contract.toml").write_text(_contract(), encoding="utf-8")
    (repository / "verification" / "toolchain.lock.toml").write_text(
        """\
schema = 1
[verus]
revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
[verification]
timeout_seconds = 60
random_seed = 0
resource_policy = "fail-closed"
""",
        encoding="utf-8",
    )
    (repository / "verification" / "manifest.toml").write_text(
        """\
schema = 1
status = "unimplemented"
assurance_claim = "none"
composition_theorem = ""
composition_members = []
shared_proof_closure = []
trusted_computing_base = ["pinned-verification-toolchain"]
assumptions = []
components = []
trusted_adapters = []
tooling = []
""",
        encoding="utf-8",
    )
    (repository / "docs" / "PRODUCT.md").write_text("# Product\n", encoding="utf-8")
    (repository / "docs" / "VERIFICATION.md").write_text("# Verification\n", encoding="utf-8")
    frozen = root / "frozen"
    for relative in (
        "verification/contract.toml",
        "verification/toolchain.lock.toml",
        "docs",
    ):
        source = repository / relative
        destination = frozen / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    return repository, frozen


def test_target_selects_obligation_but_cannot_supply_the_verus_command(tmp_path: Path) -> None:
    path = tmp_path / "contract.toml"
    path.write_text(_contract(), encoding="utf-8")
    stage = load_contract(path)
    check = stage["tasks"][0]["checks"][0]
    assert check["command"] == "oxide-verus proof --root verification/proofs/component.rs"
    assert check["receipt_required"] is False

    path.write_text(
        _contract().replace(
            'root = "verification/proofs/component.rs"',
            'root = "verification/proofs/component.rs"\ncommand = "true"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="unsupported fields"):
        load_contract(path)


def test_harness_policy_accepts_an_honest_empty_program_and_rejects_proof_escapes(
    tmp_path: Path,
) -> None:
    repository, frozen = _repository(tmp_path)
    policy, state, _ = validate_policy(repository, frozen)
    assert state["manifest"]["status"] == "unimplemented"
    assert policy["production_features"] == ["production", "verified"]

    (repository / "verification" / "models" / "escape.rs").write_text(
        "proof fn shortcut() { assume(false); }\n", encoding="utf-8"
    )
    with pytest.raises(VerificationError, match="forbidden proof escape"):
        validate_policy(repository, frozen)


def test_policy_requires_exact_manifest_ownership_for_every_fixture(tmp_path: Path) -> None:
    repository, frozen = _repository(tmp_path)
    fixture = repository / "verification" / "fixtures" / "proof-policy" / "case.toml"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('case = "forbidden escape"\n', encoding="utf-8")

    with pytest.raises(
        VerificationError,
        match=r"unclassified non-authoritative tooling: verification/fixtures/proof-policy/case.toml",
    ):
        validate_policy(repository, frozen)

    manifest = repository / "verification" / "manifest.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "tooling = []",
            """\
[[tooling]]
id = "proof-policy-fixtures"
authority = "none"
paths = ["verification/fixtures/proof-policy"]
""",
        ),
        encoding="utf-8",
    )
    _policy, state, _closure = validate_policy(repository, frozen)
    assert state["manifest"]["tooling"][0]["id"] == "proof-policy-fixtures"


def test_frozen_product_and_verification_specs_are_judge_inputs(tmp_path: Path) -> None:
    repository, frozen = _repository(tmp_path)
    (repository / "docs" / "PRODUCT.md").write_text("# Weaker product\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="immutable verification-contract input"):
        validate_policy(repository, frozen)


def test_verification_engine_digest_includes_the_evidence_schema() -> None:
    digest = engine_digest()
    assert digest.startswith("sha256:")
    assert len(digest) == 71


def test_nondefault_contract_path_is_an_explicit_engine_input(tmp_path: Path) -> None:
    repository, frozen = _repository(tmp_path)
    relative = "verification/release.toml"
    for root in (repository, frozen):
        source = root / "verification" / "contract.toml"
        value = source.read_text(encoding="utf-8").replace("verification/contract.toml", relative)
        (root / relative).write_text(value, encoding="utf-8")
        source.unlink()

    policy, state, _ = validate_policy(repository, frozen, relative)
    assert state["manifest"]["status"] == "unimplemented"
    assert policy["immutable_paths"][0] == relative

    command = invocation(repository, frozen, "policy", contract_path=relative)
    assert command[command.index("--contract") + 1] == relative
