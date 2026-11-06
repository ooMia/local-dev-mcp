"""Pinned Towncrier rendering, without workspace templates or process execution."""

from collections.abc import Mapping, Sequence

from towncrier._builder import render_fragments  # pyright: ignore[reportMissingTypeStubs]  # Pinned adapter has annotations, no stubs.

_TEMPLATE = """{% for section, categories in sections.items() %}
{% for category, entries in categories.items() %}
### {{ definitions[category]['name'] }}
{% for text in entries %}
- {{ text }}
{% endfor %}
{% endfor %}
{% endfor %}"""


def render_notes(sections: Mapping[str, Sequence[str]]) -> str:
    # Towncrier exposes no public pure renderer. Keep its pinned adapter here;
    # loading a workspace config/template would add executable project input.
    definitions = {name: {"name": name, "showcontent": True} for name in sections}
    fragments: dict[str, dict[str, dict[str, list[str]]]] = {
        "": {name: {text: [] for text in texts} for name, texts in sections.items() if texts}
    }
    return (
        render_fragments(
            _TEMPLATE, None, fragments, definitions, [""], False, {}, render_title=False, all_bullets=True
        ).strip()
        + "\n"
    )


__all__ = ["render_notes"]
