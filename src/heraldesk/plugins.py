"""Third-party platform adapters discovered through entry points.

A plugin distribution declares an entry point in the ``heraldesk.platforms`` group::

    [project.entry-points."heraldesk.platforms"]
    threads = "my_package.threads:ThreadsAdapter"

The entry point name is the platform key stored on accounts and passed to
``--platform``. The target is a zero-argument callable (a class or a factory) returning
an object that implements :class:`heraldesk.platforms.base.PlatformAdapter`.

Discovery reads installed distribution metadata only. Plugin code is imported the first
time an adapter is requested, so a broken plugin cannot stop other platforms from
working. Built-in adapters always win: a plugin cannot replace ``fake``, ``threads`` or ``nostr``.

Plugin code runs in the Heraldesk process with the permissions of the user running
Heraldesk. Install plugins you trust, exactly like any other Python dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import cast

from heraldesk.platforms.base import PlatformAdapter, PublishError

ENTRY_POINT_GROUP = "heraldesk.platforms"
BUILT_IN_PLATFORMS = frozenset({"fake", "threads", "nostr"})
_REQUIRED_METHODS = ("publish", "validate_credentials")


class PluginError(PublishError):
    """A discovered plugin is invalid or could not be imported."""


@dataclass(frozen=True, slots=True)
class PluginAdapter:
    """One discovered entry point and its deferred factory."""

    platform: str
    factory: Callable[[], PlatformAdapter]
    target: str


def _entry_points() -> Iterable[EntryPoint]:
    """Return the installed entry points for the plugin group."""
    return entry_points(group=ENTRY_POINT_GROUP)


def _build_factory(entry: EntryPoint, platform: str) -> Callable[[], PlatformAdapter]:
    def factory() -> PlatformAdapter:
        try:
            target = entry.load()
        except Exception as exc:
            raise PluginError(f"plugin '{platform}' failed to import: {exc}") from exc
        if not callable(target):
            raise PluginError(f"plugin '{platform}' target {entry.value!r} is not callable")
        try:
            adapter = target()
        except Exception as exc:
            raise PluginError(f"plugin '{platform}' factory failed: {exc}") from exc
        declared = getattr(adapter, "platform", None)
        if not isinstance(declared, str) or declared.strip().lower() != platform:
            raise PluginError(
                f"plugin '{platform}' returned adapter platform {declared!r}; "
                "the entry point name and adapter.platform must match"
            )
        missing = [name for name in _REQUIRED_METHODS if not callable(getattr(adapter, name, None))]
        if missing:
            raise PluginError(f"plugin '{platform}' is missing: {', '.join(missing)}")
        return cast(PlatformAdapter, adapter)

    return factory


def discover_plugins() -> dict[str, PluginAdapter]:
    """Return one entry per installed plugin without importing plugin code.

    Built-in keys and duplicate entry point names are ignored; the first entry wins.
    """
    discovered: dict[str, PluginAdapter] = {}
    for entry in _entry_points():
        platform = entry.name.strip().lower()
        if not platform or platform in BUILT_IN_PLATFORMS or platform in discovered:
            continue
        discovered[platform] = PluginAdapter(
            platform=platform,
            factory=_build_factory(entry, platform),
            target=entry.value,
        )
    return discovered


_CACHE: dict[str, PluginAdapter] | None = None


def plugin_adapters() -> dict[str, PluginAdapter]:
    """Return the cached plugin registry, discovering entry points on first use."""
    global _CACHE
    if _CACHE is None:
        _CACHE = discover_plugins()
    return dict(_CACHE)


def reset_plugin_cache() -> None:
    """Forget the discovered registry. Used after installing a plugin in-process."""
    global _CACHE
    _CACHE = None
