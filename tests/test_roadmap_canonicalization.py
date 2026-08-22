from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from oxide.roadmap import (
    DOCUMENT_ROOT_ANCHOR,
    ROADMAP_VALUE_SCHEMA,
    RoadmapError,
    _source_closure,
    _source_requirement_present,
    canonical_bytes,
    canonical_source_anchor,
    canonical_source_text,
    digest_bytes,
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


def _closure_for(
    specification: str,
    references: list[dict[str, str]],
) -> list[dict[str, str]]:
    return _source_closure(
        lambda path: specification.encode("utf-8") if path == "docs/specs/LANGUAGE.md" else b"",
        {"specification_root": "docs/specs"},
        references,
    )


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


def test_structured_roadmap_schema_has_no_unsupported_unique_items_keywords() -> None:
    def assert_no_unique_items(value: object) -> None:
        if isinstance(value, dict):
            assert "uniqueItems" not in value
            for item in value.values():
                assert_no_unique_items(item)
        elif isinstance(value, list):
            for item in value:
                assert_no_unique_items(item)

    assert_no_unique_items(ROADMAP_VALUE_SCHEMA)


def test_structured_roadmap_render_rejects_duplicate_stage_ids() -> None:
    roadmap = parse_roadmap(_unordered_roadmap())
    duplicate = deepcopy(roadmap["stages"][0])
    duplicate["outcome"] = "A different outcome cannot reuse the same phase identity."
    roadmap["stages"].append(duplicate)

    with pytest.raises(RoadmapError, match="roadmap stage 6 is malformed or duplicate"):
        render_roadmap_value(roadmap)


def test_structured_roadmap_render_rejects_duplicate_invariant_ids() -> None:
    roadmap = parse_roadmap(_unordered_roadmap())
    duplicate = deepcopy(roadmap["global_invariants"][-1])
    duplicate["statement"] = "A different statement cannot reuse the same invariant identity."
    roadmap["global_invariants"].append(duplicate)

    with pytest.raises(RoadmapError, match="global invariant 4 is malformed or duplicate"):
        render_roadmap_value(roadmap)


def test_structured_roadmap_render_rejects_duplicate_string_list_items() -> None:
    roadmap = parse_roadmap(_unordered_roadmap())
    stage = roadmap["stages"][0]
    stage["included_scope"].append(stage["included_scope"][0])

    with pytest.raises(RoadmapError, match=r"stage a-base\.included_scope contains duplicates"):
        render_roadmap_value(roadmap)


@pytest.mark.parametrize("owner", ["stage", "invariant"])
def test_structured_roadmap_render_rejects_duplicate_source_references(owner: str) -> None:
    roadmap = parse_roadmap(_unordered_roadmap())
    if owner == "stage":
        field = "stage a-base.source_specifications"
        sources = roadmap["stages"][0]["source_specifications"]
    else:
        field = "global invariant alpha.sources"
        sources = roadmap["global_invariants"][1]["sources"]
    sources.append(deepcopy(sources[0]))

    with pytest.raises(RoadmapError, match=rf"{field} contains duplicate source requirements"):
        render_roadmap_value(roadmap)


def test_structured_roadmap_rejects_malformed_reserved_anchor_before_rendering() -> None:
    roadmap = parse_roadmap(_unordered_roadmap())
    roadmap["stages"][0]["source_specifications"][0]["anchor"] = "oxide://document/"

    with pytest.raises(RoadmapError, match="malformed source text"):
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


def test_roadmap_parser_preserves_ordinary_canonical_title_that_looks_like_atx() -> None:
    roadmap = parse_roadmap(_unordered_roadmap().replace('anchor = "Z"', 'anchor = "# Z"'))
    anchors = {
        source["anchor"] for stage in roadmap["stages"] for source in stage["source_specifications"]
    }

    assert "# Z" in anchors
    assert "Z" not in anchors


def test_requirement_markdown_projection_is_not_reapplied_during_roadmap_roundtrips() -> None:
    raw_requirement = "AT&amp;amp;T must remain supported."
    roadmap = parse_roadmap(_unordered_roadmap(z_requirement=raw_requirement))

    for _ in range(3):
        roadmap = parse_roadmap(render_roadmap_value(roadmap))

    stored = next(
        source["requirement"]
        for invariant in roadmap["global_invariants"]
        for source in invariant["sources"]
        if source["path"] == "z.md"
    )
    assert stored == raw_requirement
    assert canonical_source_text(stored) == "AT&amp;T must remain supported."


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


def test_canonical_requirement_is_not_projected_as_markdown_twice() -> None:
    raw_requirement = "AT&amp;amp;T must remain supported."
    canonical_requirement = canonical_source_text(raw_requirement)
    specification = f"# Rules\n\n{raw_requirement}\n"
    reference = {
        "path": "docs/specs/LANGUAGE.md",
        "anchor": "Rules",
        "requirement": raw_requirement,
    }

    assert canonical_requirement == "AT&amp;T must remain supported."
    assert canonical_source_text(canonical_requirement) == "AT&T must remain supported."
    assert _source_requirement_present(
        canonical_requirement,
        specification,
        requirement_is_canonical=True,
    )
    closure = _closure_for(specification, [reference])
    assert closure[0]["requirement"] == canonical_requirement


def test_canonical_requirement_is_not_reparsed_as_an_atx_heading() -> None:
    raw_requirement = r"\# Literal leading hash must remain."
    canonical_requirement = canonical_source_text(raw_requirement)
    specification = f"# Requirements\n\n{raw_requirement}\n"
    reference = {
        "path": "docs/specs/LANGUAGE.md",
        "anchor": "Requirements",
        "requirement": raw_requirement,
    }

    assert canonical_requirement == "# Literal leading hash must remain."
    assert _source_requirement_present(
        canonical_requirement,
        specification,
        requirement_is_canonical=True,
    )
    assert _closure_for(specification, [reference])[0]["requirement"] == canonical_requirement


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


def test_document_anchor_selects_only_the_preamble_or_whole_headingless_document() -> None:
    with_heading = "Root requirement.\n\n# Later\n\nHeading requirement.\n"
    root = markdown_section(with_heading, DOCUMENT_ROOT_ANCHOR, "PRODUCT.md")

    assert root == "Root requirement.\n"
    assert _source_requirement_present("Root requirement.", root)
    assert not _source_requirement_present("Heading requirement.", root)
    assert (
        markdown_section("Only root requirement.\n", DOCUMENT_ROOT_ANCHOR, "PRODUCT.md")
        == "Only root requirement.\n"
    )


def test_reserved_locators_win_collisions_and_literal_escape_recovers_heading() -> None:
    specification = """\
Root contract.

# oxide://document

Literal document-locator heading.

# Requirements

First requirements contract.

# oxide://heading/2/Requirements

Literal lineage-locator heading.

# Requirements

Second requirements contract.
"""
    literal_document = "oxide://literal/oxide%3A%2F%2Fdocument"
    lineage = "oxide://heading/2/Requirements"
    literal_lineage = "oxide://literal/oxide%3A%2F%2Fheading%2F2%2FRequirements"

    assert "Root contract." in markdown_section(specification, DOCUMENT_ROOT_ANCHOR, "PRODUCT.md")
    assert "Literal document-locator heading." not in markdown_section(
        specification, DOCUMENT_ROOT_ANCHOR, "PRODUCT.md"
    )
    assert "Literal document-locator heading." in markdown_section(
        specification, literal_document, "PRODUCT.md"
    )
    assert "Second requirements contract." in markdown_section(specification, lineage, "PRODUCT.md")
    assert "Literal lineage-locator heading." in markdown_section(
        specification, literal_lineage, "PRODUCT.md"
    )
    assert canonical_source_anchor(literal_lineage) == literal_lineage

    emphasized_literal = "oxide://literal/%2A%2Aoxide%3A%2F%2Fdocument%2A%2A"
    with pytest.raises(ValueError, match="reserved Oxide namespace"):
        canonical_source_anchor("**oxide://document**")
    assert "Emphasized reserved title." in markdown_section(
        "# **oxide://document**\n\nEmphasized reserved title.\n",
        emphasized_literal,
        "PRODUCT.md",
    )
    with pytest.raises(RoadmapError, match="canonical representation"):
        markdown_section(
            "# **oxide://document**\n\nEmphasized reserved title.\n",
            literal_document,
            "PRODUCT.md",
        )


def test_unique_ordinary_heading_rejects_literal_and_lineage_aliases() -> None:
    specification = "# **Rules**\n\nCanonical rules contract.\n"

    assert "Canonical rules contract." in markdown_section(specification, "Rules", "PRODUCT.md")
    for alias in (
        "oxide://literal/%2A%2ARules%2A%2A",
        "oxide://heading/1/Rules",
    ):
        with pytest.raises(RoadmapError, match="canonical representation"):
            markdown_section(specification, alias, "PRODUCT.md")
        with pytest.raises(RoadmapError, match="canonical representation"):
            _closure_for(
                specification,
                [
                    {
                        "path": "docs/specs/LANGUAGE.md",
                        "anchor": alias,
                        "requirement": "Canonical rules contract.",
                    }
                ],
            )


def test_exact_canonical_title_wins_over_legacy_atx_presentation_alias() -> None:
    specification = """\
# # Rules

Shared requirement.
First-only requirement.

# Rules

Shared requirement.
Second-only requirement.
"""
    reference = {
        "path": "docs/specs/LANGUAGE.md",
        "anchor": "# Rules",
        "requirement": "Shared requirement.",
    }

    section = markdown_section(specification, "# Rules", "PRODUCT.md")
    closure = _closure_for(specification, [reference])

    assert "First-only requirement." in section
    assert "Second-only requirement." not in section
    assert closure[0]["anchor"] == "# Rules"
    assert "First-only requirement." in markdown_section(
        specification, "### # Rules ###", "PRODUCT.md"
    )


def test_repeated_exact_canonical_title_never_retargets_legacy_projection() -> None:
    specification = """\
# # Rules

Shared requirement.
First-only requirement.

# # Rules

Shared requirement.
Second-only requirement.

# Rules

Shared requirement.
Legacy-target requirement.
"""
    reference = {
        "path": "docs/specs/LANGUAGE.md",
        "anchor": "# Rules",
        "requirement": "Shared requirement.",
    }

    with pytest.raises(RoadmapError, match="canonical representation"):
        markdown_section(specification, "# Rules", "PRODUCT.md")
    with pytest.raises(RoadmapError, match="canonical representation"):
        _closure_for(specification, [reference])


def test_legacy_ordinary_alias_and_canonical_title_have_identical_closure() -> None:
    specification = "# Rules\n\nCanonical rules contract.\n"
    reference = {
        "path": "docs/specs/LANGUAGE.md",
        "anchor": "Rules",
        "requirement": "Canonical rules contract.",
    }

    canonical = _closure_for(specification, [reference])
    legacy = _closure_for(specification, [{**reference, "anchor": "## Rules ##"}])

    assert legacy == canonical
    assert (
        _closure_for(specification, [reference, {**reference, "anchor": "## Rules ##"}])
        == canonical
    )


def test_unique_reserved_heading_rejects_lineage_alias() -> None:
    specification = "# oxide://document\n\nLiteral reserved contract.\n"

    with pytest.raises(RoadmapError, match="canonical representation"):
        markdown_section(
            specification,
            "oxide://heading/1/oxide%3A%2F%2Fdocument",
            "PRODUCT.md",
        )


def test_occurrence_qualified_heading_anchor_selects_repeated_top_level_heading() -> None:
    specification = """\
# Requirements

First contract.

# Requirements

Second contract.
"""

    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section(specification, "Requirements", "PRODUCT.md")

    second = markdown_section(
        specification,
        "oxide://heading/2/Requirements",
        "PRODUCT.md",
    )
    assert _source_requirement_present("Second contract.", second)
    assert not _source_requirement_present("First contract.", second)


def test_heading_lineage_anchor_uses_same_title_sibling_occurrences() -> None:
    specification = """\
# API / surface

## Rules & limits

First API contract.

# API / surface

## Rules & limits

Second API contract.
"""
    noncanonical = "oxide://heading/2/API%20%2f%20surface/1/Rules%20%26%20limits"
    anchor = "oxide://heading/2/API%20%2F%20surface/1/Rules%20%26%20limits"

    with pytest.raises(ValueError, match="canonical URL encoding"):
        canonical_source_anchor(noncanonical)
    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section(specification, noncanonical, "PRODUCT.md")
    section = markdown_section(specification, anchor, "PRODUCT.md")
    assert _source_requirement_present("Second API contract.", section)
    assert not _source_requirement_present("First API contract.", section)


def test_atx_heading_index_ignores_matching_fences_for_lookup_and_section_extent() -> None:
    specification = """\
# First

Before the fence.

```markdown
# Hidden backtick heading
```

After the fence.

~~~~
# Hidden tilde heading
~~~
   ## Still hidden after a short closer
~~~~~

   ## Real indented heading ##

Real contract.
"""

    first = markdown_section(specification, "First", "PRODUCT.md")
    assert _source_requirement_present("After the fence.", first)
    assert not _source_requirement_present("Hidden backtick heading", first)
    assert not _source_requirement_present("Hidden tilde heading", first)
    assert "Hidden backtick heading" in first
    assert "Still hidden after a short closer" in first
    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section(specification, "Hidden backtick heading", "PRODUCT.md")
    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section(specification, "Still hidden after a short closer", "PRODUCT.md")
    assert "Real contract." in markdown_section(
        specification, "Real indented heading", "PRODUCT.md"
    )


def test_atx_heading_index_accepts_three_spaces_rejects_four_and_remains_atx_only() -> None:
    specification = """\
    # Four-space pseudo heading

Setext pseudo heading
---------------------

   # Three-space heading

ATX contract.
"""

    root = markdown_section(specification, DOCUMENT_ROOT_ANCHOR, "PRODUCT.md")
    assert "Four-space pseudo heading" in root
    assert "Setext pseudo heading" in root
    assert "ATX contract." not in root
    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section(specification, "Four-space pseudo heading", "PRODUCT.md")
    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section(specification, "Setext pseudo heading", "PRODUCT.md")
    assert "ATX contract." in markdown_section(specification, "Three-space heading", "PRODUCT.md")


def test_atx_closing_hash_requires_whitespace_so_csharp_and_c_remain_distinct() -> None:
    specification = """\
# C#

C-sharp contract.

# C #

C contract.
"""

    assert canonical_source_anchor("C#") == "C#"
    assert canonical_source_text("# C#\n\nC-sharp contract.") != canonical_source_text(
        "# C #\n\nC-sharp contract."
    )
    assert "C-sharp contract." in markdown_section(specification, "C#", "PRODUCT.md")
    assert "C contract." not in markdown_section(specification, "C#", "PRODUCT.md")
    assert "C contract." in markdown_section(specification, "C", "PRODUCT.md")


@pytest.mark.parametrize(("canonical", "legacy"), [("C#", "C"), ("F#", "F")])
def test_schema_one_trailing_hash_alias_resolves_and_normalizes_when_unambiguous(
    canonical: str,
    legacy: str,
) -> None:
    requirement = f"Implement the {canonical} adapter."
    specification = f"# {canonical}\n\n{requirement}\n"
    legacy_reference = {
        "path": "docs/specs/LANGUAGE.md",
        "anchor": legacy,
        "requirement": requirement,
    }
    canonical_reference = {**legacy_reference, "anchor": canonical}

    legacy_closure = _closure_for(specification, [legacy_reference])
    canonical_closure = _closure_for(specification, [canonical_reference])

    assert legacy_closure == canonical_closure
    assert digest_bytes(canonical_bytes(legacy_closure)) == digest_bytes(
        canonical_bytes(canonical_closure)
    )
    assert legacy_closure[0]["anchor"] == canonical
    assert _closure_for(specification, [legacy_reference, canonical_reference]) == canonical_closure


def test_schema_one_c_alias_uses_requirement_to_choose_c_or_csharp() -> None:
    specification = """\
# C

Implement the C adapter.

# C#

Implement the C-sharp adapter.
"""
    base = {"path": "docs/specs/LANGUAGE.md", "anchor": "C"}

    c_closure = _closure_for(specification, [{**base, "requirement": "Implement the C adapter."}])
    csharp_closure = _closure_for(
        specification, [{**base, "requirement": "Implement the C-sharp adapter."}]
    )
    decorated_csharp_closure = _closure_for(
        specification,
        [{**base, "anchor": "**C**", "requirement": "Implement the C-sharp adapter."}],
    )

    assert c_closure[0]["anchor"] == "C"
    assert csharp_closure[0]["anchor"] == "C#"
    assert decorated_csharp_closure == csharp_closure


def test_schema_one_c_alias_disambiguates_an_already_canonical_requirement() -> None:
    raw_requirement = "AT&amp;amp;T must remain supported."
    canonical_requirement = canonical_source_text(raw_requirement)
    specification = f"# C\n\nOther contract.\n\n# C#\n\n{raw_requirement}\n"
    reference = {
        "path": "docs/specs/LANGUAGE.md",
        "anchor": "C",
        "requirement": raw_requirement,
    }

    closure = _closure_for(specification, [reference])

    assert closure[0]["anchor"] == "C#"
    assert closure[0]["requirement"] == canonical_requirement


@pytest.mark.parametrize("canonical", ["C", "F"])
def test_canonical_language_anchor_wins_when_hash_variant_supports_same_requirement(
    canonical: str,
) -> None:
    specification = f"""\
# {canonical}

Implement the shared adapter.

# {canonical}#

Implement the shared adapter.
"""
    reference = {
        "path": "docs/specs/LANGUAGE.md",
        "anchor": canonical,
        "requirement": "Implement the shared adapter.",
    }

    closure = _closure_for(specification, [reference])

    assert closure[0]["anchor"] == canonical


def test_nested_entity_titles_resolve_without_reapplying_markdown_projection() -> None:
    specification = """\
# &amp;amp;

First entity contract.

# &amp;

Second entity contract.
"""

    first = markdown_section(specification, "&amp;", "PRODUCT.md")
    second = markdown_section(specification, "&", "PRODUCT.md")

    assert "First entity contract." in first
    assert "Second entity contract." not in first
    assert "Second entity contract." in second


def test_repeated_nested_entity_titles_use_stable_canonical_lineage_segments() -> None:
    specification = """\
# &amp;amp;

First entity contract.

# &amp;amp;

Second entity contract.
"""

    first = markdown_section(specification, "oxide://heading/1/%26amp%3B", "PRODUCT.md")
    second = markdown_section(specification, "oxide://heading/2/%26amp%3B", "PRODUCT.md")

    assert "First entity contract." in first
    assert "Second entity contract." not in first
    assert "Second entity contract." in second


def test_empty_atx_headings_participate_in_lineage_and_section_boundaries() -> None:
    specification = "#\n\nFirst empty contract.\n\n#\n\nSecond empty contract.\n"

    with pytest.raises(ValueError, match="explicit Oxide locator"):
        canonical_source_anchor("#")
    with pytest.raises(RoadmapError, match="canonical representation"):
        markdown_section(specification, "#", "PRODUCT.md")
    first = markdown_section(specification, "oxide://heading/1/", "PRODUCT.md")
    second = markdown_section(specification, "oxide://heading/2/", "PRODUCT.md")
    assert "First empty contract." in first
    assert "Second empty contract." not in first
    assert "Second empty contract." in second
    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section(specification, "oxide://literal/", "PRODUCT.md")
    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section("#\n\nOnly empty contract.\n", "oxide://literal/", "PRODUCT.md")
    assert "Only empty contract." in markdown_section(
        "#\n\nOnly empty contract.\n", "oxide://heading/1/", "PRODUCT.md"
    )
    assert "Literal hash contract." in markdown_section(
        "# \\#\n\nLiteral hash contract.\n", "#", "PRODUCT.md"
    )


def test_titles_that_project_to_empty_use_empty_heading_lineages() -> None:
    specification = """\
# <span></span>

First cosmetic-empty contract.

# <!-- hidden -->

Second cosmetic-empty contract.
"""

    first = markdown_section(specification, "oxide://heading/1/", "PRODUCT.md")
    second = markdown_section(specification, "oxide://heading/2/", "PRODUCT.md")

    assert "First cosmetic-empty contract." in first
    assert "Second cosmetic-empty contract." not in first
    assert "Second cosmetic-empty contract." in second


@pytest.mark.parametrize(
    "anchor",
    [
        "oxide://heading/0/Requirements",
        "oxide://heading/01/Requirements",
        "oxide://heading/1",
        "oxide://heading/1/%GG",
        "oxide://heading/1/%FF",
        "oxide://heading/1/%52equirements",
        "oxide://literal/%2f",
        "oxide://literal/title/child",
        "oxide://document/",
        "oxide://unknown/value",
        " oxide://document",
        "oxide://document ",
    ],
)
def test_malformed_explicit_heading_anchor_fails_closed(anchor: str) -> None:
    with pytest.raises(ValueError):
        canonical_source_anchor(anchor)
    with pytest.raises(RoadmapError, match="exactly one Markdown heading"):
        markdown_section("# Requirements\n\nContract.\n", anchor, "PRODUCT.md")


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
