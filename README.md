# Heraldesk

> Your autonomous social media department. Self-hosted, open source, single-user.

**Status: pre-alpha. Under active development. Not ready for production use.**

Heraldesk runs a small team of AI roles (strategist, researcher, writer, designer, community manager, analyst, compliance) that research, draft, review and schedule social media content for the accounts you manage.
You approve; Heraldesk does the legwork. It runs on your laptop or your own VPS. Your keys, your data.

## Planned features
- Multiple profiles (brands/people) with their own voice, rules and autonomy level
- Draft-first workflow with approval via CLI, Telegram or local web UI
- Daily content pipeline, comment triage, weekly performance report
- Adapters for Threads, Nostr, X, Facebook, Instagram, LinkedIn (official APIs only)
- Bring your own LLM key (DeepSeek, Claude, OpenAI, Gemini, OpenRouter) or local models via Ollama
- Plugin system for new platforms and agents
- No telemetry

## Quickstart (planned)
```bash
pipx install heraldesk
heraldesk init
heraldesk profile new
heraldesk serve
```

## Safety and acceptable use
Heraldesk is for managing accounts you own or are authorized to manage. Do not use it for fake-account
networks, impersonation, spam or platform-rule evasion. You are responsible for complying with each
platform's terms and local law. Optional `disclose_ai` setting adds an AI-assistance marker to content.

## Documentation
- [Architecture](docs/architecture.md)
- [Roadmap](ROADMAP.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- Bahasa Indonesia: [docs/id/arsitektur.md](docs/id/arsitektur.md)

## License
MIT. See [LICENSE](LICENSE).
