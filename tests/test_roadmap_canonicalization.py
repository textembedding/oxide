from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxide.roadmap import (
    RoadmapError,
    _source_requirement_present,
    canonical_source_text,
    markdown_section,
    parse_roadmap,
    render_roadmap_document,
    render_roadmap_value,
)


def _stage(identifier: str, dependencies: list[str], *, sources: str) -> str:
    rendered_dependencies = ", ".join(f'"{item}"' for item in dependencies)
    return f'''\
[[stages]]
id = "{identifier}"
outcome = "Deliver {identifier}."
included_scope = ["Second owned item", "First owned item"]
excluded_scope = []
dependencies = [{rendered_dependencies}]
source_specifications = [{sources}]
applicable_global_invariants = ["zeta", "oxide-verification-policy", "alpha"]
implementation_goals = ["Implement {identifier}."]
verification_goals = ["Prove {identifier} refines its contract."]
readiness = "ready"
'''


def _unordered_roadmap(*, z_requirement: str = "Z requirement.") -> str:
    z_source = '{ path = "z.md", anchor = "Z", requirement = ' + json.dumps(z_requirement) + " }"
    a_source = '{ path = "a.md", anchor = "A", requirement = "A requirement." }'
    return f"""\
<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "docs/specs"

[[global_invariants]]
id = "zeta"
statement = "Zeta invariant."
sources = [{z_source}, {a_source}]

[[global_invariants]]
id = "oxide-verification-policy"
statement = "Oxide verification policy."
sources = []

[[global_invariants]]
id = "alpha"
statement = "Alpha invariant."
sources = [{z_source}]

{_stage("a-final", ["z-child", "a-child"], sources=z_source)}
{_stage("z-child", ["a-base"], sources=f"{z_source}, {a_source}")}
{_stage("z-base", [], sources=z_source)}
{_stage("a-child", ["z-base"], sources=z_source)}
{_stage("a-base", [], sources=z_source)}
```
"""


def test_roadmap_canonicalizes_unordered_identity_collections() -> None:
    roadmap = parse_roadmap(_unordered_roadmap())

    assert [item["id"] for item in roadmap["global_invariants"]] == [
        "oxide-verification-policy",
        "alpha",
        "zeta",
    ]
    assert [item["path"] for item in roadmap["global_invariants"][2]["sources"]] == [
        "a.md",
        "z.md",
    ]

    stages = roadmap["stages"]
    assert [item["id"] for item in stages] == [
        "a-base",
        "z-base",
        "a-child",
        "z-child",
        "a-final",
    ]
    assert stages[-1]["dependencies"] == ["a-child", "z-child"]
    assert stages[0]["applicable_global_invariants"] == [
        "oxide-verification-policy",
        "alpha",
        "zeta",
    ]
    z_child = next(item for item in stages if item["id"] == "z-child")
    assert [item["path"] for item in z_child["source_specifications"]] == ["a.md", "z.md"]

    # Scope order can convey an authored narrative and is not part of canonical identity ordering.
    assert stages[0]["included_scope"] == ["Second owned item", "First owned item"]


def test_canonical_roadmap_render_is_idempotent() -> None:
    rendered = render_roadmap_document(_unordered_roadmap())

    assert render_roadmap_document(rendered) == rendered
    assert parse_roadmap(rendered) == parse_roadmap(_unordered_roadmap())


def test_structured_roadmap_render_matches_canonical_document_render() -> None:
    parsed = parse_roadmap(_unordered_roadmap())

    assert render_roadmap_value(parsed) == render_roadmap_document(_unordered_roadmap())
    assert parse_roadmap(render_roadmap_value(parsed)) == parsed


def test_roadmap_title_must_remain_nonempty_for_legacy_artifact_compatibility() -> None:
    roadmap = parse_roadmap(_unordered_roadmap())
    roadmap["title"] = ""

    with pytest.raises(RoadmapError, match="must be nonempty"):
        render_roadmap_value(roadmap)


def test_canonical_roadmap_render_reparse_preserves_structured_citations() -> None:
    requirement = """\
Admission requires:

- [x] an exact owner;
  - one nested witness;
- [ ] no pending conflict.

| Input | Result |
| --- | --- |
| accepted | durable |

```rust
assert!(owner.is_exact());
```
"""
    roadmap = _unordered_roadmap(z_requirement=requirement)
    parsed = parse_roadmap(roadmap)

    assert parse_roadmap(render_roadmap_document(roadmap)) == parsed


def test_source_markdown_cosmetic_formatting_is_equivalent() -> None:
    assert canonical_source_text(
        """\
* [X] **First** requirement wraps
    across lines.
    + Nested requirement.
1) Ordered requirement.
> Quoted *requirement* wraps
> across lines.
"""
    ) == canonical_source_text(
        """\
- [x] First requirement wraps across lines.
  - Nested requirement.
1. Ordered requirement.
> Quoted requirement wraps across lines.
"""
    )


def test_source_markdown_checkbox_state_is_semantic() -> None:
    assert canonical_source_text("- [x] Required gate") != canonical_source_text(
        "- [ ] Required gate"
    )


def test_source_markdown_nesting_and_list_kind_are_semantic() -> None:
    nested = "- Parent\n  - Child"
    flat = "- Parent\n- Child"
    ordered = "1. Parent\n2. Child"

    assert canonical_source_text(nested) != canonical_source_text(flat)
    assert canonical_source_text(flat) != canonical_source_text(ordered)


def test_source_markdown_ordered_ordinals_and_order_are_semantic() -> None:
    baseline = "1. Acquire\n2. Publish"

    assert canonical_source_text(baseline) != canonical_source_text("2. Acquire\n3. Publish")
    assert canonical_source_text(baseline) != canonical_source_text("1. Publish\n2. Acquire")


def test_source_markdown_blockquote_boundary_is_semantic() -> None:
    quoted = "> Durable evidence\n> remains authoritative."
    plain = "Durable evidence remains authoritative."

    assert canonical_source_text(quoted) != canonical_source_text(plain)


def test_source_markdown_canonical_form_is_idempotent() -> None:
    samples = [
        "Paragraph.\n\n- Item\n  - Child\n\nFollowing paragraph.",
        "| First | Second |\n| :--- | ---: |\n| one | two |",
        "```rust\n- exact code  \n> exact bytes\n```",
        "Alpha `x  y` beta",
        "> Quoted paragraph.\n\nPlain paragraph.",
    ]

    for sample in samples:
        canonical = canonical_source_text(sample)
        assert canonical_source_text(canonical) == canonical


def test_collaborative_document_fixture_is_canonicalization_idempotent() -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "eval"
        / "examples"
        / "collaborative-document"
        / "base"
        / "specs"
    )

    for specification in sorted(root.glob("*.md")):
        canonical = canonical_source_text(specification.read_text(encoding="utf-8"))
        assert canonical_source_text(canonical) == canonical, specification.name


def test_citation_match_ignores_archived_variant_like_paragraph_flattening() -> None:
    section = """\
## Purpose

This document defines the externally observable behavior of a Rust engine for
editing structured documents while replicas may be disconnected.

The engine accepts authenticated operations, preserves them durably, merges
concurrent work deterministically, and synchronizes replicas without requiring a
single online coordinator.
"""
    flattened = (
        "This document defines the externally observable behavior of a Rust engine for "
        "editing structured documents while replicas may be disconnected. The engine accepts "
        "authenticated operations, preserves them durably, merges concurrent work "
        "deterministically, and synchronizes replicas without requiring a single online "
        "coordinator."
    )

    assert _source_requirement_present(flattened, section)


def test_citation_match_ignores_unordered_marker_and_indent_width_presentation() -> None:
    section = """\
## Requirements

* Preserve every accepted record.
    + Recover the same bytes after restart.
* Reject malformed input.
"""
    requirement = """\
- Preserve every accepted record.
  - Recover the same bytes after restart.
- Reject malformed input.
"""

    assert _source_requirement_present(requirement, section)


def test_citation_match_accepts_root_ordered_list_flattened_as_prose() -> None:
    product = (
        Path(__file__).parents[1] / "eval/examples/agent-message-board/base/specs/PRODUCT.md"
    ).read_text(encoding="utf-8")
    section = markdown_section(product, "5.2 Validation order", "PRODUCT.md")
    flattened = (
        "The service evaluates publication failure conditions in this order: "
        "1. request framing and canonical decoding; "
        "2. board authentication; "
        "3. session generation validity; "
        "4. capability signature and authority epoch; "
        "5. idempotency binding; "
        "6. record schema and size limits; "
        "7. proposed RecordId uniqueness within the batch and admitted board; "
        "8. topic, concept, task, and access authorization; "
        "9. external causal-reference existence and visibility; "
        "10. internal causal-order validity; "
        "11. typed coordination predecessor validity; "
        "12. configured capacity admission; "
        "13. durable commit. "
        "The first failing class determines the public error. Later checks MUST NOT change "
        "externally visible state after an earlier failure."
    )

    assert _source_requirement_present(flattened, section)
    assert not _source_requirement_present(flattened.replace("12.", "14.", 1), section)
    assert not _source_requirement_present(
        flattened.replace(
            "11. typed coordination predecessor validity; 12. configured capacity admission; ",
            "12. configured capacity admission; 11. typed coordination predecessor validity; ",
        ),
        section,
    )


def test_citation_match_accepts_quote_callout_text_but_preserves_its_boundary() -> None:
    section = """\
## Capacity

Before the invariant.

> For every inventory unit, held quantity never exceeds admitted capacity.

After the invariant.
"""

    assert _source_requirement_present(
        "For every inventory unit, held quantity never exceeds admitted capacity.",
        section,
    )
    assert not _source_requirement_present(
        "Before the invariant. For every inventory unit, held quantity never exceeds admitted "
        "capacity.",
        section,
    )
    assert not _source_requirement_present(
        "For every inventory unit, held quantity never exceeds admitted capacity. After the "
        "invariant.",
        section,
    )


def test_citation_match_preserves_checkbox_ordinal_nesting_and_code_semantics() -> None:
    section = """\
## Admission

- [x] Freeze the candidate.
  - Bind the exact tree.
1. Execute `cargo  test`.
2. Publish the receipt.
"""

    assert not _source_requirement_present("- [ ] Freeze the candidate.", section)
    assert not _source_requirement_present(
        "- [x] Freeze the candidate.\n- Bind the exact tree.", section
    )
    assert not _source_requirement_present("2. Execute `cargo  test`.", section)
    assert not _source_requirement_present("1. Execute `cargo test`.", section)


def test_citation_match_accepts_an_isolated_exact_table_row() -> None:
    specification = """\
## State transitions

| Current | Operation | Next | Additional guard |
| :--- | :---: | --- | ---: |
| absent | `PLACE_HOLD` | held | all lines valid and capacity sufficient |
| held | `CONFIRM` | confirmed | authorization validates |

## Other transitions

| Current | Operation | Next | Additional guard |
| --- | --- | --- | --- |
| confirmed | `SETTLE` | settled | capture observation validates |
"""
    section = markdown_section(specification, "State transitions", "DEVELOPMENT.md")

    assert _source_requirement_present(
        "absent|`PLACE_HOLD`|held|all lines valid and capacity sufficient", section
    )
    assert _source_requirement_present(
        "| absent | `PLACE_HOLD` | held | all lines valid and capacity sufficient |",
        section,
    )
    assert not _source_requirement_present(
        "| absent | `PLACE_HOLD` | confirmed | all lines valid and capacity sufficient |",
        section,
    )
    assert not _source_requirement_present(
        "| `PLACE_HOLD` | absent | held | all lines valid and capacity sufficient |",
        section,
    )
    assert not _source_requirement_present("| absent | `PLACE_HOLD` | held |", section)
    assert not _source_requirement_present(
        "| absent | `PLACE_HOLD` | held | all lines valid and capacity sufficient | extra |",
        section,
    )
    assert not _source_requirement_present(
        "| absent | `PLACE_HOLD` | held | all lines valid and capacity sufficient |\n"
        "| held | `CONFIRM` | confirmed | authorization validates |",
        section,
    )
    assert not _source_requirement_present(
        "| confirmed | `SETTLE` | settled | capture observation validates |",
        section,
    )


def test_transactional_transition_rows_are_admissible_atomic_citations() -> None:
    specification = (
        Path(__file__).parents[1]
        / "eval/examples/transactional-reservation/base/specs/DEVELOPMENT.md"
    ).read_text(encoding="utf-8")
    section = markdown_section(specification, "6. State transition table", "DEVELOPMENT.md")
    rows = [
        "| absent | `PLACE_HOLD` | held | all lines valid and capacity sufficient |",
        "| held | `CONFIRM` | confirmed | operation time is before expiration and authorization validates |",
        "| held | `CANCEL` | cancelled | caller is authorized |",
        "| held | `EXPIRE_DUE` | expired | operation time is at or after expiration |",
        "| confirmed | `SETTLE` | settled | capture observation validates |",
        "| confirmed | `CANCEL` | cancelled | frozen policy permits release and no nonterminal capture attempt exists |",
        "| settled | `REFUND` | settled | cumulative refund remains bounded |",
        "| settled | `CANCEL` | cancelled | refund is complete, no refund outcome is unresolved, and frozen policy permits release |",
    ]

    assert all(_source_requirement_present(row, section) for row in rows)


def test_citation_match_rejects_omission_reordering_and_paraphrase() -> None:
    section = """\
## Recovery

Recovery authenticates the image, validates its complete prefix, and publishes
the selected generation atomically.

- Retain the prior generation.
- Install the selected generation.
- Release the prior generation.
"""

    assert not _source_requirement_present(
        "Recovery authenticates the image and publishes the selected generation atomically.",
        section,
    )
    assert not _source_requirement_present(
        "Recovery validates its complete prefix, authenticates the image, and publishes the "
        "selected generation atomically.",
        section,
    )
    assert not _source_requirement_present(
        "Recovery checks the image and atomically makes the chosen generation visible.",
        section,
    )
    assert not _source_requirement_present(
        "- Retain the prior generation.\n- Release the prior generation.", section
    )


def test_citation_match_cannot_cross_heading_boundary() -> None:
    section = """\
## Recovery

The selected image is authenticated.

### Publication

The selected generation is published atomically.
"""

    assert not _source_requirement_present(
        "The selected image is authenticated. The selected generation is published atomically.",
        section,
    )
