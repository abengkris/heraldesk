"""Safe rendering of generated Markdown reports."""

from __future__ import annotations

import bleach
import markdown as markdown_lib

_ALLOWED_TAGS = [
    "a",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
]
_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel"],
    "th": ["align"],
    "td": ["align"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def render_markdown(text: str) -> str:
    """Render Markdown to HTML and strip scripts, event handlers and unsafe URLs."""
    raw = markdown_lib.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html",
    )
    cleaned = bleach.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Every link opens outside the app and cannot access window.opener.
    return cleaned.replace("<a ", '<a rel="noopener noreferrer" ')
