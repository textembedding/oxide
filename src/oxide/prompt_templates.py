"""Packaged Jinja templates for interactive agent prompts."""

from __future__ import annotations

from functools import cache
from importlib.resources import files

from jinja2 import StrictUndefined, Template, TemplateError


class PromptTemplateError(RuntimeError):
    pass


_TEMPLATE_FILES = {
    "planning": "planning.md.j2",
    "planning-follow-up": "planning-follow-up.md.j2",
    "contract-generation": "contract-generation.md.j2",
    "contract-follow-up": "contract-follow-up.md.j2",
}


@cache
def _template(name: str) -> Template:
    try:
        filename = _TEMPLATE_FILES[name]
    except KeyError as error:
        raise PromptTemplateError(f"unknown prompt template: {name}") from error
    try:
        source = files("oxide").joinpath("prompts", filename).read_text(encoding="utf-8")
        return Template(
            source,
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
    except (OSError, TemplateError) as error:
        raise PromptTemplateError(f"cannot load prompt template {filename}: {error}") from error


def render_prompt(name: str, **values: object) -> str:
    """Render one known prompt and fail closed on absent injected values."""
    try:
        return _template(name).render(**values)
    except TemplateError as error:
        raise PromptTemplateError(f"cannot render prompt template {name}: {error}") from error


def render_prompt_source(source: str, **values: object) -> str:
    """Render an in-memory candidate with the production template policy."""
    try:
        template = Template(
            source,
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        return template.render(**values)
    except TemplateError as error:
        raise PromptTemplateError(f"cannot render candidate prompt: {error}") from error
