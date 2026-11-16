"""Offline fake LLM provider.

The fake provider is what every test and CI run uses. It returns fixture JSON per agent
role, estimates token usage deterministically, and can be scripted with a response
sequence or a forced failure to exercise retries and fail-closed paths.

Fixture lookup order: an explicit ``fixtures`` mapping, then ``fixture_dir/<role>.json``,
then the bundled ``heraldesk/llm/fixtures/<role>.json``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from heraldesk.llm.errors import LLMError
from heraldesk.llm.types import LLMRequest, LLMResult

BUNDLED_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DEFAULT_COST_PER_1K_TOKENS = 0.002


def estimate_tokens(text: str) -> int:
    """Return a stable, cheap token estimate (one per whitespace-separated word)."""
    return max(1, len(text.split()))


def _read_json_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LLMError(f"cannot read fake fixture {path}: {exc}") from exc


@dataclass
class FakeLLM:
    """Deterministic provider used by tests and by ``llm.provider: fake``."""

    name: str = "fake"
    default_model: str = "fake/fake"
    fixtures: Mapping[str, str] | None = None
    fixture_dir: Path | None = None
    responses: Sequence[str] = ()
    fail_with: LLMError | None = None
    cost_per_1k_tokens: float = DEFAULT_COST_PER_1K_TOKENS
    calls: list[LLMRequest] = field(default_factory=list)
    _response_index: int = field(default=0, init=False, repr=False)

    def complete(self, request: LLMRequest) -> LLMResult:
        """Return the next scripted response, else the fixture for the agent role."""
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        text = self._next_text(request.agent)
        tokens_in = sum(estimate_tokens(message.content) for message in request.messages)
        tokens_out = estimate_tokens(text)
        cost = round((tokens_in + tokens_out) / 1000 * self.cost_per_1k_tokens, 6)
        return LLMResult(
            text=text,
            model=request.model or self.default_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )

    def _next_text(self, role: str) -> str:
        if self.responses:
            index = min(self._response_index, len(self.responses) - 1)
            self._response_index += 1
            return self.responses[index]
        fixture = self._fixture_text(role)
        if fixture is None:
            raise LLMError(
                f"no fake fixture for agent {role!r}; add {role}.json to the fixture folder"
            )
        return fixture

    def _fixture_text(self, role: str) -> str | None:
        if self.fixtures is not None:
            return self.fixtures.get(role)
        if self.fixture_dir is not None:
            override = _read_json_text(self.fixture_dir / f"{role}.json")
            if override is not None:
                return override
        return _read_json_text(BUNDLED_FIXTURES_DIR / f"{role}.json")


def json_fixture(payload: object) -> str:
    """Serialize a fixture payload for the ``responses`` sequence."""
    return json.dumps(payload, ensure_ascii=False)
