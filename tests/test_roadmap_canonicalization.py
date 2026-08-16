from __future__ import annotations

from oxide.roadmap import canonical_source_text, parse_roadmap, render_roadmap_document


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


def _unordered_roadmap() -> str:
    z_source = '{ path = "z.md", anchor = "Z", requirement = "Z requirement." }'
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
