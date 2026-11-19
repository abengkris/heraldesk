> **Terjemahan ini tertinggal dari implementasi.** Dokumen berbahasa Inggris
> [`docs/architecture.md`](../architecture.md) adalah sumber kebenaran. Beberapa bagian di
> bawah masih menggambarkan rencana lama (Tailwind, aiogram, argon2, Docker/PyPI yang
> belum ada, dan lain-lain). Gunakan versi Inggris dan `ROADMAP.md` untuk status terkini.

# Heraldesk: Arsitektur Teknis (Departemen Social Media Manager Otonom, Open Source, Single-User, Self-Hosted)

Dokumen ini adalah spesifikasi arsitektur Heraldesk untuk dikerjakan kontributor, manusia maupun coding agent. Baca seluruhnya sebelum menulis kode. Kerjakan per milestone di bagian 13, jangan melompat.

## 1. Tujuan

Aplikasi open source yang dijalankan sendiri oleh satu pengguna, di laptop/PC (lokal) atau VPS miliknya. Aplikasi menjalankan "departemen social media" otonom (Strategist, Researcher, Writer, Designer, Community Manager, Analyst, Compliance) untuk satu atau beberapa akun/brand yang dikelola pengguna. Manusia hanya meninjau dan menyetujui.

Antarmuka: CLI, Telegram, dan Web UI lokal. Tidak ada layanan cloud milik proyek, tidak ada akun, tidak ada telemetri.

### Prinsip desain (wajib dipatuhi)

1. **Instalasi semudah mungkin.** Satu perintah (`pipx install` / `uvx` / `docker run`), tanpa database server, tanpa broker, tanpa Node di runtime.
2. **Satu proses.** `heraldesk serve` menjalankan API, web, scheduler, worker, dan bot Telegram sekaligus.
3. **Workflow deterministik, bukan agen bebas.** Alur ditulis sebagai pipeline dengan state machine. LLM dipakai di dalam langkah, bukan menentukan alur.
4. **Aman secara default.** Setiap profil dimulai dalam mode draft-only. Auto-post harus diaktifkan eksplisit dan dibatasi tingkat risiko. Server hanya mendengarkan `127.0.0.1` secara default.
5. **Bring your own key (BYOK).** Pengguna memakai API key LLM sendiri, atau model lokal (Ollama). Tidak ada vendor yang dikunci.
6. **Hackable.** Identitas agen, prompt, dan kebijakan adalah file teks yang bisa diedit pengguna, bukan kode.
7. **Semua tindakan tercatat** di audit log lokal.
8. **Output agen selalu terstruktur** (Pydantic), divalidasi, dan di-retry bila tidak valid.

## 2. Tech Stack

| Lapisan | Pilihan |
|---|---|
| Bahasa | Python 3.12 |
| API/Web server | FastAPI + Uvicorn |
| Web UI | Jinja2 + HTMX + Tailwind (CSS sudah di-build dan di-commit, tanpa Node saat runtime) |
| Database | SQLite (mode WAL), via SQLAlchemy 2.x + Alembic. Kode tidak boleh memakai fitur khusus SQLite yang menghalangi pindah ke Postgres kelak |
| Scheduler | APScheduler (in-process); job persisten di tabel `jobs` agar lanjut setelah restart |
| LLM | LiteLLM (DeepSeek, Claude, OpenAI, Gemini, OpenRouter, Ollama) |
| CLI | Typer + Rich |
| Telegram | aiogram atau python-telegram-bot |
| Konfigurasi | YAML + `.env` (pydantic-settings) |
| Secrets | Enkripsi at-rest dengan Fernet; kunci di `secret.key` (izin 600) atau keyring OS |
| Log | structlog (JSON atau konsol) |
| Test | pytest, pytest-asyncio, respx, fake LLM provider |
| Distribusi | PyPI (`heraldesk`), image Docker tunggal, lisensi MIT (atau Apache-2.0, pemilik proyek yang memutuskan) |
| Dependency | `uv` + lockfile, versi dipin |

## 3. Arsitektur

```
        CLI | Telegram | Web UI (HTMX) | REST API lokal
                         │
                ┌────────▼────────┐        satu proses: `heraldesk serve`
                │   Core services │  profiles, content, approvals, inbox
                └───┬─────────┬───┘
         ┌──────────▼──┐   ┌──▼───────────┐
         │ Orchestrator│   │  Scheduler   │ (APScheduler + tabel jobs)
         │ (pipelines) │   └──┬───────────┘
         └──────┬──────┘      │
         ┌──────▼──────┐   ┌──▼───────────┐
         │Agent Runtime│   │  Publisher   │──► Threads / Nostr / X / FB /
         │ (roles+LLM) │   │  (adapters)  │     IG / LinkedIn / ...
         └──────┬──────┘   └──────────────┘
                └────────┬────────┘
                    ┌────▼─────┐
                    │  SQLite  │ + folder data (file profil)
                    └──────────┘
```

### Struktur folder data (default `~/.heraldesk/`, bisa diganti dengan `HERALDESK_HOME`)

```
heraldesk.yaml            # konfigurasi global (LLM, server, Telegram)
heraldesk.db              # state runtime (SQLite)
secret.key             # kunci enkripsi kredensial (jangan di-commit)
profiles/<slug>/
  soul.md              # identitas, nada, hal yang selalu/tidak pernah dikatakan
  brief.md             # brand brief, audiens, tujuan
  policy.yaml          # kebijakan otonomi dan jadwal posting
  banned.yaml          # kata dan topik terlarang, disclaimer wajib
  voice/               # contoh tulisan asli (txt/md) untuk few-shot
agents/                # override opsional prompt per peran (*.md)
logs/
backups/
```

**Sumber kebenaran:** file di `profiles/` adalah definisi profil (bisa di-git oleh pengguna). SQLite menyimpan state runtime (konten, approval, inbox, metrik, audit). Perintah `heraldesk profile reload` memvalidasi file dan memuatnya; kesalahan skema ditampilkan jelas.

### Repo

```
heraldesk/
  AGENTS.md  README.md  LICENSE  CONTRIBUTING.md  SECURITY.md
  pyproject.toml  uv.lock  Dockerfile  docker-compose.yml
  alembic/
  src/heraldesk/
    config.py  paths.py
    db/  core/(crypto, audit, locks)  domain/
    llm/  agents/(base, strategist, researcher, writer, designer,
                  community, analyst, compliance, prompts/*.md)
    pipelines/(daily_content, reply_triage, weekly_report, onboarding)
    platforms/(base, fake, threads, nostr, x, facebook, instagram, linkedin)
    scheduler/  api/  web/(templates, static)  cli/  telegram/
    plugins.py          # loader plugin via entry points
  tests/  docs/  examples/
```

## 4. Model Data (SQLite)

Semua tabel yang relevan punya `profile_id`. Isolasi antar-profil dijaga di lapisan repository: setiap query wajib melewati `ProfileScope(profile_id)`, dan agen hanya menerima konteks satu profil. Ini mencegah bocornya konteks antar-brand bila satu pengguna mengelola beberapa akun.

- **profiles**: id, slug, name, type (`company|person|public_figure|other`), timezone, languages, status (`active|paused`), kill_switch, config_hash
- **social_accounts**: id, profile_id, platform, handle, credentials_encrypted, status, last_checked_at
- **content_ideas**: id, profile_id, source, title, angle, evidence (JSON), status
- **content_items**: id, profile_id, account_id, idea_id, platform, body, media (JSON), state, risk_level, risk_notes, scheduled_at, published_at, external_id, parent_id, created_by_run_id
- **approvals**: id, content_item_id, decision (`approve|reject|edit`), edited_body, reason, decided_at, channel (`web|telegram|cli`)
- **inbox_items**: id, account_id, kind (`comment|mention|dm`), external_id, author, text, sentiment, intent, status
- **replies**: id, inbox_item_id, body, state, risk_level
- **metrics_snapshots**: id, content_item_id, captured_at, metrics (JSON)
- **memories**: id, profile_id, kind (`fact|preference|lesson|trend`), text, source_run_id
- **agent_runs**: id, profile_id, agent, pipeline_run_id, input, output, model, tokens_in, tokens_out, cost_usd, status, error, started_at, finished_at
- **pipeline_runs**: id, profile_id, pipeline, trigger, status, started_at, finished_at
- **audit_log**: id, profile_id, actor (`user|agent:<name>|system`), action, entity, entity_id, payload, created_at. Append-only (dijaga di level service, tidak ada operasi update/delete).
- **jobs**: id, kind, payload, run_at, status, attempts, last_error, idempotency_key

### State machine `content_items.state`

```
idea → drafted → reviewed → pending_approval → approved → scheduled → published
                    │               │                         │
                    ▼               ▼                         ▼
                 blocked         rejected                   failed (retry ≤ 3)
```

Transisi hanya lewat `domain.content.transition()` yang memvalidasi aturan dan menulis audit log. Dilarang mengubah `state` dari tempat lain.

## 5. Agent Runtime

### Kelas dasar
Setiap agen punya `role`, daftar `tools` (allowlist), dan output schema Pydantic. Dijalankan lewat `AgentRunner.run(agent, input)` yang: merakit prompt, memanggil LLM, memvalidasi schema, retry maksimal 2 kali, mencatat `agent_runs` beserta token dan biaya.

Prompt sistem dirakit dari: template peran (`agents/prompts/<role>.md`, bisa di-override pengguna di folder `agents/`) + `soul.md` + `banned.yaml` + batasan platform + memori relevan.

### Peran dan kontrak

| Agen | Input | Output | Tools |
|---|---|---|---|
| Strategist | profil, metrik 30 hari, kalender | `ContentPlan` (slot, pilar, platform, tujuan) | baca metrik, kalender |
| Researcher | pilar, kompetitor, tanggal | `list[Idea]` (judul, sudut, bukti, sumber) | web search, fetch URL |
| Writer | ide + profil + platform | `Draft` (body, varian, hashtag) | baca voice samples |
| Designer | draft | `VisualBrief` (deskripsi visual, alt text) | opsional generator gambar |
| Community | inbox item + profil | `Triage` dan `ReplyDraft` | baca riwayat komentar |
| Analyst | metrik, konten | `WeeklyReport` (temuan, rekomendasi, pelajaran) | baca metrik |
| Compliance | draft atau balasan | `RiskAssessment` (`low|medium|high|blocked`, alasan, perbaikan) | cek banned list, lint |

Compliance berjalan pada SETIAP draft dan balasan sebelum masuk antrean approval. Level `blocked` tidak boleh dipublikasikan tanpa diedit dan dinilai ulang.

Pengguna dapat menonaktifkan peran yang tidak dibutuhkan lewat `heraldesk.yaml` (misalnya Designer), kecuali Compliance dan lint yang selalu aktif.

### Lint antislop (deterministik, sebelum Compliance LLM)
`quality.lint(text, profile)` menolak: em dash, buzzword dari daftar, sapaan generik, klaim berlebihan, kata terlarang profil. Draft gagal dikembalikan ke Writer maksimal 2 kali, lalu ditandai untuk review manusia. Aturan lint bisa dikonfigurasi per profil.

## 6. Pipeline

**`daily_content`** (terjadwal per profil): Strategist tentukan slot → Researcher beri ide (tolak yang mirip konten 60 hari terakhir) → Writer tulis draft per platform → Designer (opsional) → lint → Compliance → terapkan kebijakan otonomi → `pending_approval` atau `approved` → notifikasi (Telegram/web/terminal).

**`reply_triage`** (polling berkala): ambil komentar/mention baru → Community mengklasifikasi (spam, pujian, pertanyaan, keluhan, krisis) → spam diabaikan, pujian dan pertanyaan dibuatkan draft, keluhan atau krisis dieskalasi ke manusia tanpa draft yang bisa terkirim otomatis → Compliance → kebijakan otonomi.

**`weekly_report`**: Analyst menyusun laporan, menyimpan pelajaran ke `memories`, mengirim ringkasan.

**`onboarding`** (`heraldesk profile new`): wizard interaktif dari brief, tautan akun, dan contoh tulisan; menghasilkan `soul.md`, `banned.yaml`, `policy.yaml` draft. Pengguna meninjau dan mengedit file sebelum profil `active`.

## 7. Kebijakan Otonomi

`profiles/<slug>/policy.yaml`:

```yaml
default_mode: draft_only
rules:
  - { action: post,  risk: low,    mode: auto_after_delay, delay_minutes: 30 }
  - { action: post,  risk: medium, mode: require_approval }
  - { action: post,  risk: high,   mode: require_approval }
  - { action: reply, risk: low, intents: [praise], mode: auto }
  - { action: reply, risk: medium, mode: require_approval }
quiet_hours: { start: "21:00", end: "07:00" }
max_posts_per_day: 3
```

Aturan:
- `blocked` tidak pernah lolos.
- Topik di `banned_topics` menaikkan risiko ke `high`.
- `auto_after_delay` memberi jendela pembatalan sebelum terbit.
- `kill_switch` per profil dan global (`heraldesk stop-all`) menghentikan semua publish dan balasan seketika.
- Pengguna bisa menyetujui, menolak dengan alasan, atau mengedit. Edit disimpan sebagai memori `preference` untuk belajar gaya.
- Batas biaya LLM harian (`max_daily_cost_usd` di `heraldesk.yaml`); bila terlampaui, pipeline berhenti dan pengguna diberi tahu.

## 8. Platform Adapter dan Plugin

```python
class PlatformAdapter(Protocol):
    platform: str
    async def validate_credentials(self, account) -> bool: ...
    async def publish(self, account, item) -> PublishResult: ...
    async def fetch_interactions(self, account, since) -> list[Interaction]: ...
    async def reply(self, account, interaction, body: str) -> PublishResult: ...
    async def fetch_metrics(self, account, external_ids) -> list[Metrics]: ...
    def constraints(self) -> PlatformConstraints: ...   # panjang maks, media, rate limit
```

Aturan:
- Adapter hanya berkomunikasi dengan API; tanpa logika bisnis.
- Rate limit dan retry dengan backoff eksponensial di lapisan bersama.
- Publish idempoten (kunci idempotensi per `content_item`).
- **Hanya API resmi.** Bila fitur tidak didukung, kembalikan `NotSupported`. Tidak ada scraping atau otomasi browser yang melanggar ToS.
- Urutan implementasi: `fake`, `threads`, `nostr`, lalu `x`, `facebook`, `instagram`, `linkedin`.
- **Plugin:** adapter dan agen tambahan dapat dipasang sebagai paket terpisah lewat Python entry points (`heraldesk.platforms`, `heraldesk.agents`). Sediakan template plugin di `examples/`.

## 9. Antarmuka

**CLI** (perintah `heraldesk`, dengan alias pendek `hdesk`)
```
heraldesk init                      # wizard: folder data, kunci, LLM, Telegram
heraldesk doctor                    # cek konfigurasi, kunci, koneksi, izin file
heraldesk serve [--host --port --no-web --no-telegram]
heraldesk profile new|list|reload|pause|resume|killswitch
heraldesk account add|test <profile>
heraldesk run daily <profile> [--dry-run]
heraldesk queue list|approve|reject|edit
heraldesk inbox list|triage
heraldesk report weekly <profile>
heraldesk runs <profile>            # jejak run dan biaya
heraldesk backup | restore <file>
heraldesk stop-all
```

**Telegram:** notifikasi draft dengan tombol Setujui / Tolak / Edit / Tunda; `/status`, `/pause`, `/stopall`, briefing pagi. Hanya chat ID yang ada di allowlist `heraldesk.yaml` yang dilayani.

**Web UI (lokal):** antrean approval (diff draft, risiko, alasan), kalender konten, inbox, laporan, editor file profil dan kebijakan dengan validasi, log audit, biaya LLM. Server-rendered (HTMX), tanpa build frontend.

**REST API:** semua fungsi lewat `/api/v1/...` dengan OpenAPI; web dan Telegram memakainya atau memanggil service yang sama.

## 10. Keamanan untuk Self-Hosting

- **Bind default `127.0.0.1`.** Bila `--host` selain loopback, aplikasi menolak start kecuali password admin sudah diset, dan `heraldesk doctor` memperingatkan agar memakai reverse proxy HTTPS (sertakan contoh Caddy di `docs/`).
- **Auth tunggal:** password admin (argon2) dan sesi cookie bertanda tangan, proteksi CSRF, rate limit login. Token API pribadi untuk skrip.
- **Kredensial akun media sosial** terenkripsi at-rest; hanya didekripsi di dalam adapter saat dipakai; tidak pernah masuk prompt, log, atau output agen. Log menyamarkan nilai rahasia.
- **Prompt injection:** teks komentar, DM, dan halaman web adalah data tidak tepercaya. Dibungkus delimiter eksplisit; agen Community tidak punya tool publish; instruksi di dalam data diabaikan; publikasi hanya lewat service yang menerapkan kebijakan.
- **Allowlist tool per peran.** Hanya modul Publisher yang boleh menulis ke platform.
- **Tanpa telemetri, tanpa phone-home.** Satu-satunya lalu lintas keluar: provider LLM yang dikonfigurasi, API platform, dan pencarian web milik Researcher.
- **Retensi:** DM dan data komentar dihapus otomatis setelah N hari (default 30, bisa diubah).
- **Supply chain:** dependensi dipin dan di-lock, `pip-audit` di CI, `SECURITY.md` berisi cara melapor kerentanan.
- **Kebijakan penggunaan (di README dan CONTRIBUTING):** proyek ini untuk mengelola akun milik sendiri atau milik pihak yang memberi izin. Tidak untuk jaringan akun palsu, impersonasi, atau spam. Opsi `disclose_ai: true` per profil untuk menambahkan penanda konten dibantu AI. Pengguna bertanggung jawab atas kepatuhan terhadap ToS platform dan hukum setempat.

## 11. Observability dan Testing

- Log terstruktur dengan `profile_id`, `pipeline_run_id`, `agent_run_id`.
- Ringkasan biaya (token dan USD) per profil, agen, dan hari.
- **Fake LLM provider** dengan keluaran dari fixture, sehingga seluruh pipeline dapat dites tanpa biaya dan tanpa internet.
- Contract test untuk setiap PlatformAdapter terhadap `fake`.
- Tes state machine: transisi legal dan ilegal.
- Tes isolasi profil: konteks dan query profil A tidak pernah memuat data profil B.
- Set evaluasi kecil untuk Compliance (aman, berisiko, terlarang) dengan ambang akurasi minimum.
- CI: ruff, mypy, pytest, pip-audit, build wheel dan image Docker pada Linux, macOS, dan Windows untuk tes inti.

## 12. Dukungan Platform Operasi

- Linux, macOS, Windows (WSL atau native untuk CLI). Termux/Android sebagai target "best effort".
- Docker: satu image, volume untuk `HERALDESK_HOME`, contoh `docker-compose.yml` dan unit `systemd` untuk VPS.
- Upgrade: `heraldesk upgrade` menjalankan migrasi Alembic dan otomatis membuat backup terlebih dahulu.
- Backup: `heraldesk backup` menyalin `heraldesk.db` (dengan SQLite online backup API), folder `profiles/`, dan `heraldesk.yaml`. `secret.key` hanya disertakan bila diminta eksplisit.

## 13. Roadmap Implementasi (berurutan)

Setiap milestone selesai bila kriteria penerimaan lolos dan ada tes.

**M0: Fondasi dan packaging**
Repo, `pyproject` + `uv`, struktur folder, `paths.py`, `config.py`, `heraldesk init` dan `doctor`, SQLite + Alembic, CI.
*Penerimaan:* `pipx install .` lalu `heraldesk init` dan `heraldesk doctor` berjalan di Linux dan macOS; CI hijau.

**M1: Domain inti**
Profil dari file (validasi skema, `reload`), akun media sosial dengan kredensial terenkripsi, audit log, kill switch, `ProfileScope`.
*Penerimaan:* tes isolasi profil lolos; `heraldesk profile new/list` dan `account add/test` bekerja.

**M2: LLM layer dan Agent Runtime**
LiteLLM wrapper, structured output + retry, pencatatan `agent_runs` dan biaya, fake provider, kelas Agent dasar, prompt sebagai file `.md` dengan override pengguna.
*Penerimaan:* satu agen berjalan dengan fake dan provider asli; biaya tercatat; batas biaya harian berfungsi.

**M3: Pipeline `daily_content` dengan publisher palsu**
Researcher, Writer, Compliance, `quality.lint`, state machine, antrean.
*Penerimaan:* `heraldesk run daily <profile> --dry-run` menghasilkan draft dengan state benar; semua run tercatat.

**M4: Approval dan Telegram**
Layanan approval, kebijakan otonomi (`policy.yaml`), bot Telegram dengan tombol, edit dan reject, allowlist chat ID.
*Penerimaan:* draft muncul di Telegram, disetujui, berpindah ke `approved`; edit tersimpan sebagai memori preferensi.

**M5: Scheduler, publisher, adapter pertama**
APScheduler + tabel `jobs` persisten, adapter `threads` dan `nostr`, idempotensi, retry.
*Penerimaan:* post terjadwal terbit di akun uji; restart proses tidak kehilangan job; retry tidak menggandakan posting.

**M6: Community dan Analyst**
`reply_triage`, eskalasi krisis, pengambilan metrik, `weekly_report`.
*Penerimaan:* komentar uji terklasifikasi dan melewati kebijakan; laporan mingguan terkirim.

**M7: Web UI lokal**
Antrean approval, kalender, inbox, laporan, editor profil dan kebijakan, audit, biaya; auth password.
*Penerimaan:* seluruh alur approval dapat dilakukan dari web; start dengan host non-loopback tanpa password ditolak.
*Catatan repositori (2026-11-17):* pass M7 pertama mengimplementasikan server satu proses: FastAPI
`/health`, `/status` dan `/webhook/telegram`, sweep latar APScheduler dan polling Telegram. Web UI,
auth password dan namespace `/api/v1` tetap pekerjaan berikutnya. Lihat `docs/decisions.md`.

**M8: Plugin dan adapter tambahan**
Loader entry points, template plugin, adapter X, Facebook, Instagram, LinkedIn.
*Penerimaan:* plugin pihak ketiga contoh terpasang dan terdeteksi tanpa mengubah kode inti.

**M9: Rilis v0.1.0**
Image Docker, publikasi PyPI, `docker-compose.yml` dan `systemd` contoh, dokumentasi (quickstart lokal, deploy VPS + Caddy, menulis `soul.md`, menulis plugin), `SECURITY.md`, `CONTRIBUTING.md`, contoh profil di `examples/`.
*Penerimaan:* pengguna baru dapat dari nol sampai draft pertama dalam kurang dari 15 menit mengikuti README.

## 14. Di Luar Lingkup

Multi-user dan multi-tenant, penagihan, hosting cloud resmi, app mobile native, generasi video, auto-DM outreach, scraping platform tanpa API resmi.

## 15. Panduan untuk Kontributor (Manusia dan Coding Agent)

- Kerjakan satu milestone per sesi: rencana singkat dulu, lalu kode, lalu jalankan tes.
- Jangan menambah dependensi besar tanpa alasan; catat keputusan di `docs/decisions.md`.
- Type hint di semua fungsi publik; `mypy --strict` pada `src/heraldesk/core` dan `src/heraldesk/domain`.
- Jangan pernah menaruh kredensial, API key, atau data akun nyata di repo atau fixture.
- Jaga kesederhanaan instalasi: tolak solusi yang mewajibkan layanan eksternal (Redis, Postgres, Node) di runtime.
- Bila spesifikasi ambigu atau bertentangan dengan kenyataan API platform, berhenti dan tulis pertanyaan di `docs/open-questions.md`, jangan menebak.
- Salin bagian 1 (prinsip) dan 15 ke `AGENTS.md` di root repo.
