# Copy-edit suggestions (proposed)

Status: **idea only.** Nothing in this document is implemented. It records a future
quality feature without committing it to a milestone.

## Problem

Weak or free models produce typos and occasional invented words. A generic Indonesian
proofreader or KBBI-style normalizer is not an acceptable fix: it turns casual brand voice
into formal copy.

Examples of changes that must **not** happen automatically:

| Draft | Formalizer would change it to | Why that is wrong |
|---|---|---|
| `nggak` | `tidak` | Changes register |
| `banget` | `sangat` | Changes intensity and voice |
| `aku` / `kamu` | `saya` / `Anda` | Changes relationship with the audience |
| code-switching | one language only | Erases the profile's actual voice |
| fragments and lowercase openings | full sentences | Makes the brand sound corporate |

A typo is different from a style choice. `pesaikannya` → `pesaingnya` is a correction.
`nggak` → `tidak` is a rewrite.

## Goals

- Catch objective errors: typos, duplicated words, broken punctuation, malformed URLs.
- Preserve slang, register, code-switching, fragments, casing and rhythm.
- Show a diff; never silently rewrite the body.
- Keep the human as the editor of record.
- Stay optional and off by default.

## Non-goals

- KBBI or formal-grammar normalization.
- Synonym or tone substitution.
- Fact-checking names, dates, claims or titles.
- Translation.
- Auto-applying a suggestion.
- Blocking approval or publishing when the copy-edit call fails.

## Product rules

1. `copyedit: off | suggest` per profile, default `off`.
2. A suggestion is a diff plus one-line reasons, shown next to the draft.
3. The human chooses **Apply** or **Dismiss**; nothing changes until Apply.
4. Applying a suggestion uses the existing queue edit path and records an audit entry.
5. The copy-edit step never changes content state, approval state or scheduling.
6. Failure is non-blocking: the draft remains exactly as written.
7. Profile files (`soul.md`, `brief.md`, voice samples) are authoritative over the editor.
8. The step is never a pipeline gate; it is a suggestion available on demand.

## Two layers

### Deterministic checks first

Cheap, predictable, no model:

- repeated words (`itu itu`)
- double spaces, stray punctuation, unbalanced quotes
- a per-profile typo map, for example `pesaikannya: pesaingnya`
- URL and handle shape checks

A typo map is explicit and reviewable. It never touches voice. It will not catch every
error, but it cannot flatten a persona.

### Voice-aware LLM suggestion

Only for ambiguous cases the deterministic layer cannot decide. The prompt must include:

- the profile's `soul.md`, `brief.md` and voice samples;
- the declared register (casual, formal, mixed);
- an explicit do-not-change list: slang, code-switching, fragments, lowercase style,
  emoji, hashtags and handles;
- an instruction to change only clear errors that affect meaning or correctness;
- a requirement to return a machine-readable diff and reasons.

The model returns suggestions, not a replacement body. It must not receive or use platform
comments, DMs or other untrusted text.

## UX

Queue detail and the Draft Room artifact card:

```text
Copy-edit suggestion
- ... enggan 'membela' pesaikannya.
+ ... enggan 'membela' pesaingnya.
Reason: likely typo; meaning unchanged.

[Apply] [Dismiss]
```

- `Apply` edits the draft through the normal edit service, then re-runs lint and
  Compliance if the change is material.
- `Dismiss` records the decision and leaves the draft untouched.
- The approval action remains separate. A copy-edit suggestion is not an approval.

## Configuration

Proposed per-profile policy field:

```yaml
# profiles/<slug>/policy.yaml
copyedit: suggest   # off | suggest
```

A global default can stay `off`. No separate model configuration in the first version;
the same `llm.default_model` is used. Per-role models are a separate future feature.

## Audit

- `copyedit.suggested` — draft id, source (`rules` or `llm`), number of hunks
- `copyedit.applied` — draft id, old hash, new hash
- `copyedit.dismissed` — draft id, reason (optional)

Audit payloads store hashes and counts, not full draft bodies.

## Acceptance criteria for a future implementation

- [ ] Off by default; enabling it is explicit per profile.
- [ ] No suggestion is applied without a human action.
- [ ] A slang-only draft (`nggak`, `banget`, `aku`) produces no style changes.
- [ ] A known typo produces a diff with a reason.
- [ ] Applying a suggestion uses the existing edit service and audit path.
- [ ] A failed LLM call leaves the draft unchanged and does not block approval.
- [ ] The step never changes state, approval or scheduling.
- [ ] Tests cover a casual Indonesian voice, a formal voice and a mixed-language voice.
- [ ] The queue and Draft Room both show the same diff model.

## Why this is deferred

The immediate fix for typos is a stronger Writer model and human review. A proofreading
step adds another LLM call, another failure path and a risk of voice drift. It is worth
building only as a suggestion layer with an explicit voice contract, not as a mandatory
pipeline stage.
