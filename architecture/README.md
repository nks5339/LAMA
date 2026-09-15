# Archify for LAMA

**From repository to architecture diagram.**

A setup walkthrough for installing Archify in this repository and regenerating the five
verifiable diagrams of LAMA's real architecture that live in this folder.

| | |
|---|---|
| **Guide by** | Nikunj |
| **Tool** | [Archify](https://github.com/tt-a1i/archify) v2.17 — open source, MIT, by `tt-a1i` |
| **Primary agent** | Claude Code |
| **Install scope** | Project-only (`.claude/skills/archify`) |

> vendored here unmodified. Its own `SKILL.md` remains the authority on the tool; where this guide
> and Archify's `SKILL.md` disagree, `SKILL.md` wins.

---

## 1 — What Archify does

Archify is an **agent skill**, not a standalone app. You describe a system or point it at a
repository, the agent writes a typed JSON intermediate representation (IR), and Archify
deterministically compiles that IR into a polished, self-contained HTML/SVG diagram that opens in
any browser.

- **Five diagram types** — `architecture`, `workflow`, `sequence`, `dataflow`, `lifecycle`.
- **Dark & light themes** — every diagram ships with both, plus four visual presets.
- **Explorable output** — search nodes, trace reach, probe routes, compare roles, play guided
  chapters, all grounded in the authored topology.
- **Portable exports** — PNG, JPEG, WebP, dual-theme SVG, WebM, and three 1200×630 card variants.

**Prerequisite:** Node.js. Verified here on **v26.8.2** (Archify requires ≥ 18).

---

## 2 — Install

Installed at **project scope**, so the skill is versioned with this repo:

```bash
npx skills add tt-a1i/archify -a claude-code -s '*' --copy -y
```

Two notes the upstream guide does not make explicit:

- Omitting `-g` selects project scope. `-g` installs globally to `~/.claude/skills/`.
- The agent identifier is **`claude-code`**, not `claude`. Passing `claude` is rejected.

Verify:

```bash
node .claude/skills/archify/bin/archify.mjs doctor
```

Expect `Archify is ready.` and 15 `[ok]` lines.

### Two side effects of installing

The `skills` CLI changes two files in the repo root — worth knowing before you commit:

- **`.gitignore` gains a `.claude/` rule.** The 8.4 MB vendored skill is therefore **not committed**.
  Anyone cloning this repo must re-run the install command above before they can regenerate a
  diagram. The diagrams themselves are committed and need no tooling to read.
- **`skills-lock.json` is created**, pinning `tt-a1i/archify` by content hash. Commit it so the
  install is reproducible.

The optional update check runs at most once every 72 hours and never downloads anything. Disable
it with `export ARCHIFY_UPDATE_CHECK_DISABLED=1`.

---

## 3 — The five LAMA diagrams

| File (`src/`) | Type | What it shows |
|---|---|---|
| `lama-runtime.architecture.json` | architecture | Runtime topology: SPA → nginx → FastAPI → Mongo/Qdrant, and the LLM fabric's outbound transports |
| `lama-pipeline.workflow.json` | workflow | The five freeze-gated stages, the handoff that unlocks each one, and the skip bypass |
| `lama-fabric-call.sequence.json` | sequence | One `fabric_call()` from a stage route to a provider and back, including the health gate |
| `lama-discovery-kb.dataflow.json` | dataflow | Legacy source → parse/extract → Mongo + Qdrant → `context_bundler` → SRS |
| `lama-codegen-multiagent.lifecycle.json` | lifecycle | The multi-agent CodeGen state machine and its two human confirmation gates |

Each flow has exactly **one** interactive `diagrams/<name>.html` and **one**
light-theme still `diagrams/<name>.png`; machine-readable receipts live in
`evidence/`.

> `visual-check` emits four screenshots per diagram (1440×900 and 2048×1320,
> each light and dark) plus a contact sheet. Keeping all of them meant four
> near-identical pictures of the same structure, which confuses more than it
> helps — so the 1440×900 light render is promoted to `<name>.png` and the rest
> are pruned. `build-handbook.sh` re-applies this, so re-running `visual-check`
> cannot reintroduce the duplicates. The full multi-viewport, multi-theme
> evidence is still recorded in `evidence/*.visual-check.json`.

### The handbook

[`handbook.pdf`](handbook.pdf) is the printable companion to these diagrams — a
17-page architecture manual covering what LAMA is, its technology stack with
pinned versions, the runtime topology, the pipeline and its freeze gates, the
LLM fabric and its provider probe order, the knowledge base, multi-agent
CodeGen, the data model, the API surface, deployment, the test and lint bars,
configuration, and the known footguns. All five diagrams are embedded.

It is authored in [`handbook.html`](handbook.html), which shares its stylesheet
with `docs/setup-guide.html` so both PDFs carry the same visual identity.
Rebuild it with:

```bash
./architecture/build-handbook.sh
```

---

## 4 — Regenerating a diagram

The upstream guide stops at `validate`. That is **not** the acceptance path — `deliver` is. The
full loop:

```bash
S=.claude/skills/archify

# 1. Validate while iterating. --quality showcase is required:
#    without it you get 4 basic checks instead of the 9 showcase checks.
node $S/bin/archify.mjs validate architecture architecture/src/lama-runtime.architecture.json \
     --quality showcase --json

# 2. Deliver — the final acceptance command. Freezes the spec bytes, renders,
#    checks, atomically commits the HTML, and reports SHA-256 for both.
node $S/bin/archify.mjs deliver architecture architecture/src/lama-runtime.architecture.json \
     architecture/diagrams/lama-runtime.html --quality showcase --json

# 3. Collect browser evidence from the exact delivered file.
node $S/bin/archify.mjs visual-check architecture/diagrams/lama-runtime.html --json
```

Swap the type and paths for the other four. Acceptance is **9/9 artifact checks, 0 composition
errors, 0 warnings**, then a zero exit from `deliver`, then a `pass` from `visual-check`.

**If `deliver` fails it preserves the previous HTML.** Do not run `visual-check` on that path — it
would inspect the stale last-good artifact and report a misleading pass.

Not sure which type fits a new diagram?

```bash
node .claude/skills/archify/bin/archify.mjs guide "Show an API request with a cache miss" --json
```

---

## 5 — Authoring rules that kept these diagrams readable

Learned from the diagnostics while building this set:

- **≤ 12 primary nodes, one obvious main path.** Put supporting detail in **cards**, never in
  extra edges.
- **Omit `meta.visual_preset`.** Diagrams then open in `classic`, the cleanest draw.io-like look.
- **Start with automatic routes.** Add `labelDx`/`labelDy`/`route` only when a diagnostic names
  that exact edge, and apply one control per repair.
- **Watch the desktop-readability rule.** A projected font below 6px fails. It is governed by
  `930 / viewBox_width`, so a *wider* viewBox shrinks text — for `sequence`, `column_fit: "spread"`
  is the sanctioned fix before shortening any semantic label.
- **Watch vertical overflow.** The page must fit 1440×900. A tall, narrow viewBox produces a tall
  panel; widening the aspect ratio and reducing card rows are the two effective levers.
- **`sources` needs `meta.repository`** with a real HTTP(S)/SSH address and a 40-char revision.
  This repo has no remote, so these diagrams carry no source pins — the `file:line` evidence lives
  in [`report.md`](report.md) instead.

Hard limits worth knowing before you design a layout: `workflow` columns are ranks **0–5**;
`dataflow` allows at most **5 stages**; `lifecycle` main-rail columns are **0–4** with the
event/terminal column *N* sitting beneath main column *N + 2*.

---

## 6 — Reading a generated diagram

Open any file in `diagrams/` directly in a browser. No server, no network, no dependencies.

Press **`T`** to toggle dark/light. The layout is identical in both themes; only the palette
changes. Every binding below was verified against the generated HTML in this folder:

| Key | Action |
|---|---|
| `?` | Open the Diagram Guide |
| `/` | Find and focus a node |
| `R` | Probe a directed route |
| `L` | Compare semantic roles (Lens) |
| `M` | Open the overview radar |
| `P` / `[` `]` | Play a guided chapter / previous / next |
| `F` | Presentation Stage |
| `S` / `T` / `E` | Cycle preset / toggle theme / open Export |
| `+` `-` `0` | Zoom in, out, reset |

**Visual presets** (set via `meta.visual_preset`): `classic` *(default when omitted)*,
`signal-flow`, `blueprint`, `editorial`.

---

## 7 — Troubleshooting

**Skill not found.** Confirm `.claude/skills/archify/SKILL.md` exists, then restart the Claude Code
session.

**Generation fails validation.** Get the machine-readable diagnostic:

```bash
node .claude/skills/archify/bin/archify.mjs doctor
node .claude/skills/archify/bin/archify.mjs validate architecture path/to/file.json --json
```

For `workflow` geometry specifically, use the layout receipt instead:

```bash
node .claude/skills/archify/bin/archify.mjs validate workflow path/to/file.json --layout-json
```

**Diagram HTML is ~800 KB each.** Expected — the renderer inlines the font, styles and runtime so
the file is genuinely self-contained.

---

## Credits

Archify is open source (MIT), maintained at [`tt-a1i/archify`](https://github.com/tt-a1i/archify).
This guide and the five LAMA diagrams were written and compiled by **Nikunj**.
