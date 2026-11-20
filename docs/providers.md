# LLM providers

Heraldesk reaches language models through [LiteLLM](https://docs.litellm.ai/). The
provider-specific HTTP, retries and response parsing live in LiteLLM; Heraldesk supplies
the model name, the timeout, the structured-output request and the cost accounting.

Two settings control this:

- `llm.provider` in `heraldesk.yaml`: `auto` (default), `litellm` or `fake`.
- `llm.default_model` in `heraldesk.yaml`: a LiteLLM model string, usually
  `<provider>/<model>`.

`auto` uses the offline fake provider when the model is empty or starts with `fake/`, and
LiteLLM otherwise. Set `provider: litellm` explicitly when you want a real provider call
even if the model name looks unusual.

## Where keys go

Keys belong in `$HERALDESK_HOME/.env` or the process environment, never in
`heraldesk.yaml`:

```dotenv
DEEPSEEK_API_KEY=your-key-here
```

Heraldesk exports the file into the process environment before building the provider. A
real environment variable always wins over the same name in `.env`. `heraldesk doctor`
checks the file permissions; keep them at `600`. Keys are never printed or logged, and
`.env` is always excluded from backups, including credential backups.

## Common providers

Model IDs change often. The examples below show the shape, not a permanent catalog; check
the provider's own model list.

| Provider | `llm.default_model` | Credential |
|---|---|---|
| OpenAI | `openai/<model>` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/<model>` | `ANTHROPIC_API_KEY` |
| Google Gemini | `gemini/<model>` | `GEMINI_API_KEY` |
| DeepSeek | `deepseek/<model>` | `DEEPSEEK_API_KEY` |
| Groq | `groq/<model>` | `GROQ_API_KEY` |
| Mistral | `mistral/<model>` | `MISTRAL_API_KEY` |
| OpenRouter | `openrouter/<vendor>/<model>` | `OPENROUTER_API_KEY` |
| Ollama | `ollama/<model>` | none; optional `OLLAMA_API_BASE` |
| OpenAI-compatible gateway | `openai/<model>` | `OPENAI_API_KEY` plus `OPENAI_API_BASE` |

For OpenRouter, keep the vendor segment: `openrouter/deepseek/deepseek-chat`, not
`openrouter/deepseek-chat`.

For a local Ollama server the default endpoint is `http://127.0.0.1:11434`; set
`OLLAMA_API_BASE` only when it runs elsewhere.

## Custom OpenAI-compatible endpoints

Many gateways expose an OpenAI-compatible `/v1/chat/completions` API. LiteLLM treats the
`openai/` prefix as "use the OpenAI client", so point that client at the gateway with
environment variables:

```dotenv
# $HERALDESK_HOME/.env
OPENAI_API_KEY=your-gateway-key
OPENAI_API_BASE=https://gateway.example/v1
```

```yaml
# $HERALDESK_HOME/heraldesk.yaml
llm:
  provider: litellm
  default_model: openai/<model-id>
```

LiteLLM accepts either `OPENAI_API_BASE` or `OPENAI_BASE_URL`. Heraldesk does not need a
separate base-URL field; the environment is the supported mechanism. The base URL must
include the version segment (`/v1` for most gateways) but not `/chat/completions`.

If `OPENAI_API_KEY` is already set in your shell, it wins over `.env` and the gateway key
is ignored. Unset it in that terminal first, or use a separate `HERALDESK_HOME`.

Because `OPENAI_API_KEY` is shared by every `openai/` model, do not mix a gateway key and
a real OpenAI key in the same `.env` unless you intend the base URL to apply to both.
Use a separate `HERALDESK_HOME` or a real environment variable per process when you need
both.

This also works for vLLM, LM Studio, LiteLLM Proxy and other OpenAI-compatible servers.
The model must accept the request Heraldesk sends; see [Structured output](#structured-output).

## OpenCode Console

OpenCode Console provides an OpenAI-compatible endpoint:

```text
https://opencode.ai/inference/openai/v1
```

```dotenv
# $HERALDESK_HOME/.env
OPENAI_API_KEY=your-console-api-key
OPENAI_API_BASE=https://opencode.ai/inference/openai/v1
```

```yaml
# $HERALDESK_HOME/heraldesk.yaml
llm:
  provider: litellm
  default_model: openai/deepseek-v4.1-flash
```

Other Console model IDs that work through the same endpoint include `glm-5.3-flash`,
`minimax-m3`, `gpt-6-luna` and `qwen3.8-max`. Use the bare model ID from the
[Console model list](https://opencode.ai/console/models), not the `opencode/<model>`
prefix that OpenCode itself uses.

Two restrictions matter:

- **An OpenCode Go subscription is not enough.** Go requires a coding-agent client that
  sends its own user agent and session ID. Heraldesk sends ordinary OpenAI requests, so
  Go models are refused. Use a Console API key with pay-as-you-go credit instead.
- **Free-tier models require OpenCode itself.** They cannot be called from Heraldesk.
  Choose a paid model.

Console bills the account balance. LiteLLM has no Console-specific price entry, so unless
it happens to recognise the model ID, Heraldesk records each run at `$0.00` and
`llm.max_daily_cost_usd` will not trip. Set a budget or keep a small balance in the
Console account if you need a hard limit.

## Structured output

Agents request JSON with `response_format={"type": "json_object"}` and validate the result
against a Pydantic model, retrying when validation fails. Most hosted providers support
this. Some local servers and gateways ignore or reject the field, and a few models return
prose instead of JSON. If a run fails with repeated validation errors, try a different
model before assuming a Heraldesk bug.

## Cost tracking

Heraldesk asks LiteLLM for the price of each completion. LiteLLM knows the public prices
of the providers in its catalog; for an unknown model or a custom endpoint it returns no
price and Heraldesk records `$0.00`. In that case the daily cost cap is not a real
spending limit. Use the provider's own budget controls.

## Checking the configuration

```bash
heraldesk doctor --online
```

The `llm provider` row makes one minimal completion (`max_tokens=1`) when the resolved
provider is LiteLLM. It prints only the exception class; it never prints the key, the
prompt or the response. The fake provider makes no network call.

Common failures:

| Symptom | Likely cause |
|---|---|
| `AuthenticationError` | Key missing from `.env`, revoked, or from another account. |
| `NotFoundError` or a model error | Wrong model ID, wrong prefix, or a model the key cannot access. |
| `RateLimitError` | Provider quota or credits exhausted. |
| Repeated schema validation errors | Model does not honour JSON mode or is too small for the task. |
| Connection error | Wrong `OPENAI_API_BASE`, or a local server that is not running. |

`heraldesk doctor --online` reports the provider stage only; run the command that failed
for the model-level error.

## Offline use

Leave `llm.default_model` empty, or set it to `fake`, to use the deterministic fake
provider. Every test and CI job uses it; it needs no key and makes no network call.
