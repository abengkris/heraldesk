"""Keep the hand-written CSS and the templates in sync.

The test collects literal class names used in templates and ``app.js``, class selectors
defined in ``app.css``, and a small allowlist of classes generated at runtime. It fails when
a template uses a class with no rule, when the stylesheet drifts away from the templates, or
when a retired legacy class comes back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.core

WEB = Path(__file__).resolve().parents[1] / "src" / "heraldesk" / "web"
CSS = WEB / "static" / "app.css"
TEMPLATES = WEB / "templates"
APP_JS = WEB / "static" / "app.js"

# Classes produced by macros or Python rather than written literally in a template.
DYNAMIC_CLASSES = {
    "status-ok",
    "status-warn",
    "status-danger",
    "status-info",
    "status-muted",
    "cap-ok",
    "cap-warn",
    "cap-danger",
    "diff-line",
    "diff-lines",
    "diff-no",
    "del",
    "ins",
    "htmx-indicator",
    "htmx-request",
}

LEGACY_CLASSES = {
    "stat",
    "stat-label",
    "stat-value",
    "danger-text",
    "card.stat",
    "container",
    "preview",
    "chat",
    "message",
    "artifact",
    "killswitch-chip",
    "active",
    "critical",
    "detail-body",
    "edit-preview",
    "platform",
    "profile-tabs",
    "grid",
}


def defined_classes() -> set[str]:
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    return set(re.findall(r"\.([A-Za-z_-][\w-]*)", css))


def used_classes() -> set[str]:
    used: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        for match in re.finditer(r'class="([^"]*)"', path.read_text(encoding="utf-8")):
            value = re.sub(r"\{\{.*?\}\}", " ", match.group(1))
            value = re.sub(r"\{%.*?%\}", " ", value)
            for token in value.split():
                if re.fullmatch(r"[A-Za-z_-][\w-]*", token) and not token.endswith("-"):
                    used.add(token)
    script = APP_JS.read_text(encoding="utf-8")
    for match in re.finditer(r"classList\.(?:add|remove|toggle)\(([^)]*)\)", script):
        used.update(re.findall(r'["\']([\w-]+)["\']', match.group(1)))
    return used


def test_every_used_class_is_defined() -> None:
    defined = defined_classes()
    missing = sorted(used_classes() - defined - DYNAMIC_CLASSES)
    assert not missing, f"classes used but not defined in app.css: {missing}"


def test_no_unused_class_rules() -> None:
    defined = defined_classes()
    used = used_classes() | DYNAMIC_CLASSES
    unused = sorted(defined - used)
    assert not unused, f"CSS classes defined but never used: {unused}"


def test_retired_legacy_classes_do_not_return() -> None:
    defined = defined_classes()
    used = used_classes()
    assert not (defined & LEGACY_CLASSES)
    assert not (used & LEGACY_CLASSES)
