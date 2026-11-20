"""Provider selection, configurable fake fixtures and dotenv precedence."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from heraldesk.config import load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.llm.errors import LLMConfigError, LLMError
from heraldesk.llm.factory import build_llm, export_dotenv_keys, resolve_model
from heraldesk.llm.fake import FakeLLM
from heraldesk.llm.litellm_provider import LiteLLMProvider
from heraldesk.llm.types import ChatMessage, LLMRequest

pytestmark = pytest.mark.core


def write_config(initialized: InitResult, body: str) -> None:
    initialized.layout.config_path.write_text(body, encoding="utf-8")


def test_auto_uses_fake_for_an_empty_or_fake_model(initialized: InitResult) -> None:
    config = load_config(initialized.layout.home)
    provider = build_llm(config, initialized.layout)
    assert isinstance(provider, FakeLLM)
    assert resolve_model(config, provider) == "fake/fake"

    write_config(
        initialized,
        "version: 1\nllm:\n  provider: auto\n  default_model: fake/writer\n",
    )
    config = load_config(initialized.layout.home)
    provider = build_llm(config, initialized.layout)
    assert isinstance(provider, FakeLLM)
    assert resolve_model(config, provider) == "fake/writer"


def test_auto_selects_litellm_for_a_real_model_without_calling_it(
    initialized: InitResult,
) -> None:
    write_config(
        initialized,
        "version: 1\nllm:\n  provider: auto\n  default_model: deepseek/deepseek-chat\n",
    )
    config = load_config(initialized.layout.home)
    provider = build_llm(config, initialized.layout)
    assert isinstance(provider, LiteLLMProvider)
    assert resolve_model(config, provider) == "deepseek/deepseek-chat"


def test_fake_fixtures_are_selectable_from_config(initialized: InitResult) -> None:
    fixture_dir = initialized.layout.home / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "writer.json").write_text(
        json.dumps({"body": "Custom fixture body.", "variants": [], "hashtags": []}),
        encoding="utf-8",
    )
    write_config(
        initialized,
        "version: 1\nllm:\n  provider: fake\n  fake_fixtures_dir: fixtures\n",
    )
    config = load_config(initialized.layout.home)
    provider = build_llm(config, initialized.layout)
    assert isinstance(provider, FakeLLM)

    result = provider.complete(
        LLMRequest(
            messages=(ChatMessage(role="user", content="write something"),),
            model="fake/fake",
            agent="writer",
        )
    )

    assert json.loads(result.text)["body"] == "Custom fixture body."
    assert result.tokens_in > 0
    assert result.tokens_out > 0
    assert result.cost_usd > 0


def test_missing_fixture_folder_is_a_config_error(initialized: InitResult) -> None:
    write_config(
        initialized,
        "version: 1\nllm:\n  provider: fake\n  fake_fixtures_dir: nope\n",
    )
    config = load_config(initialized.layout.home)
    with pytest.raises(LLMConfigError) as caught:
        build_llm(config, initialized.layout)
    assert "fake_fixtures_dir" in str(caught.value)


def test_environment_wins_over_dotenv(initialized: InitResult, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-environment")
    monkeypatch.setattr(os, "environ", dict(os.environ))
    (initialized.layout.home / ".env").write_text(
        "DEEPSEEK_API_KEY=from-dotenv\nOPENROUTER_API_KEY=from-dotenv-openrouter\n",
        encoding="utf-8",
    )

    exported = export_dotenv_keys(initialized.layout)

    assert exported == ("OPENROUTER_API_KEY",)
    assert os.environ["DEEPSEEK_API_KEY"] == "from-environment"
    assert os.environ["OPENROUTER_API_KEY"] == "from-dotenv-openrouter"


def test_litellm_drops_unsupported_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    module = SimpleNamespace()

    def import_module(name: str) -> SimpleNamespace:
        assert name == "litellm"
        return module

    monkeypatch.setattr("heraldesk.llm.litellm_provider.importlib.import_module", import_module)
    provider = LiteLLMProvider(model="openai/gpt-6-luna")

    assert provider._litellm() is module
    assert module.drop_params is True
    assert module.telemetry is False


@pytest.mark.parametrize(
    ("status", "fragment"),
    (
        (401, "authentication failed"),
        (402, "insufficient provider balance"),
        (403, "provider denied access"),
        (404, "model not found"),
        (429, "rate limit"),
        (503, "provider server error"),
        (418, "provider rejected the request"),
    ),
)
def test_litellm_reports_provider_status_codes(status: int, fragment: str) -> None:
    class ProviderError(Exception):
        status_code = status

    def fail(**kwargs: object) -> None:
        raise ProviderError

    provider = LiteLLMProvider(model="openai/gpt-6-luna")
    provider._module = SimpleNamespace(completion=fail, completion_cost=lambda **kw: 0.0)

    with pytest.raises(LLMError, match=fragment):
        provider.complete(
            LLMRequest(
                messages=(ChatMessage(role="user", content="hi"),),
                model="openai/gpt-6-luna",
            )
        )
