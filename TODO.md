# raspi v0.3 — implementation roadmap

Plan source: `~/.claude/plans/i-need-you-to-noble-globe.md` (approved 2026-05-04).
Goal: rebuild this agent into something as close to "Claude Code Opus 4.7 on a
Pi 5" as physically possible. Type `raspi` on a fresh Pi → working agent.

User decisions (locked):
- **Scope:** big rewrite, breakages OK. Bumps to v0.3.0.
- **Models:** `qwen2.5-coder:1.5b` (fast) + `qwen3:4b` (reasoner). Doctor probes at install.
- **Bootstrap:** hash-verified `install.sh` + `uv tool install`. First-5-things panel on success.
- **Extras (all in v0.3):** bubblewrap auto-detect sandbox, token streaming, context compaction, ast-edit via tree-sitter (Python+JS only).

---

## Architecture target

```
raspi (CLI)
├── Plan phase    → todo_write() writes <ws>/.raspi/plan.md, kept in system prompt
├── Act loop      → MAX_TOOL_LOOPS=12, smarter dedupe, per-tier escalation
│   ├── Streaming  → stream_chat() wired into final-answer turn
│   ├── Sandbox    → bash auto-wrapped in bwrap if installed
│   ├── Edit ladder → ast_edit → apply_patch → edit (single-replace) → write
│   └── Cancel     → Ctrl-C plumbed agent → Brain → httpx → tool subprocess
├── Reflect phase → every 4 loops, no-tools turn picks {keep|replan|give-up}
├── Compact phase → if history > 80% budget, fast-tier summarizes oldest half
└── Memory
    ├── WIKI.md (canonical, dream-rewritten nightly; new ## Cheatsheet section)
    ├── plan.md (per-session, ephemeral, gitignored)
    └── scratchpad.md (per-session, ephemeral, gitignored)
```

Tool registry refactored to carry `source` field so v0.4 can plug in MCP without
touching the loop.

---

## v0.3.0 — sequencing (3 weeks of work, ship in 3 chunks)

### Week 1 — "feels different" core

- [ ] **Rename CLI + package + env vars**
  - `pyproject.toml`: add `raspi`, `raspi-dream` entry points; keep `rasp` as
    deprecated alias for one release.
  - `git mv rasp_agent/ raspi_agent/`. Update all imports.
  - `config.py`: `_env("RASPI_X", "RASP_X", default)` shim. Doc both names; new
    code uses `RASPI_*`.
- [ ] **Plan-Act-Reflect loop**
  - Refactor `agent.py:Agent.turn` → `_plan(user_msg)`, `_act_loop()`,
    `_reflect()`, `_compact_if_needed()`.
  - Keep `_pick_tier`, `_context_history`, `last_turn_stats`, `_validate_tool_args`.
  - New tool `todo_write({steps, note?})` → writes `<workspace>/.raspi/plan.md`,
    injected into system prompt below the wiki, capped at 1500 chars. Pin user
    prompt verbatim at top of plan.md (model can't edit prefix).
  - `MAX_TOOL_LOOPS = 12` (was 3). `MAX_REPEAT_CALLS` stays 1 but compare on
    `(name, normalized_args)` (whitespace-stripped, default-arg-toggling-stripped,
    quote-variant-normalized).
  - `_reflect()` every 4 loops: no-tools turn returns `{"keep","replan","give_up"}`
    + optional new plan text. Cap output at 200 tokens. Never free-form.
  - Auto-escalation tied to plan-step failures, not raw tool errors.
  - Rewrite `prompts/system.md` to teach Plan-Act-Reflect with concrete examples.
- [ ] **Streaming + cancellation**
  - Wire `OllamaClient.stream_chat` (`llm.py:260`) into the **final-answer turn
    only** (tool-call turns stay non-streaming).
  - Render via `rich.live.Live`. Show `[fast|reasoner | 5.2 tok/s]` footer.
  - Cancellation token: `threading.Event` plumbed `agent.turn → Brain.chat →
    httpx.stream`. Ctrl-C cancels chat AND any in-flight `subprocess.Popen`.
  - Session-level rolling counter shown in REPL header:
    `[session: 47k tok | 12 turns | dream in 2h]`.
- [ ] **JSON-repair fallback (no constrained decoding)**
  - Skip outlines/llguidance/xgrammar — Ollama HTTP doesn't expose logits.
  - Keep `_try_extract_tool_calls_from_text` (`llm.py:102`) as-is.
  - Add `_repair_tool_json(raw)`: strip trailing commas, balance braces/fences,
    retry `json.loads`. ~30 lines stdlib.
  - Add tool-call lint recovery turn: when args fail validation, re-invoke with
    error appended as user-role correction. Cap 1 retry per loop.

### Week 2 — edits, sandbox, compaction, memory v2

- [ ] **Edit ladder**
  - New `tools/edit_ladder.py`. Order of preference:
    1. `ast_edit({path, language, query, replacement})` — tree-sitter Python+JS.
       **Risk gate:** install script verifies `tree-sitter`, `tree-sitter-python`,
       `tree-sitter-javascript` ARM64 wheels. If any forces Rust build, ship
       without ast_edit; doctor reports it disabled.
    2. `apply_patch({path, patch})` — unified diff via stdlib `difflib`.
       Validates hunks before apply.
    3. Existing `edit` (single replace) — keep `tools/fs.py`.
    4. Existing `write` — last resort.
  - Track per-tool success rate in `Store.kv` keyed `tool_success:<name>`.
    Expose in doctor.
  - Split `search` → `glob({pattern, path})` and `grep({query, path})`. Old
    `search` alias for one release.
- [ ] **Bubblewrap sandbox**
  - Keep existing `ShellGate` parser/allowlist as **first** layer.
  - Add bwrap as **execution** layer. Auto-detect: if `which bwrap`, default-on.
    `RASPI_SANDBOX=off` opts out.
  - bwrap args: `--ro-bind / / --bind <ws> <ws> --ro-bind ~/.gitconfig
    ~/.gitconfig --proc /proc --dev /dev --tmpfs /tmp --unshare-net
    --die-with-parent`. Add `--share-net` only for `curl`/`wget` after existing
    private-IP check.
  - Skip `~/.ssh` mount entirely.
  - Doctor: `bwrap --version` + `git status` smoke run.
- [ ] **Context compaction**
  - In `_compact_if_needed()`: when total chars in `_context_history()` exceeds
    80% of `RASPI_HISTORY_PROMPT_CHARS`, summarize oldest half via fast tier
    into one synthetic assistant turn `[compacted N turns: ...]`. Replace those
    turns in-memory only — SQLite log untouched.
  - Add `compactions` count to `last_turn_stats`.
- [ ] **Memory v2**
  - Add `## Cheatsheet` to `DEFAULT_HEADINGS` in `memory/wiki.py:16`. Dream
    pass auto-extracts useful shell snippets from successful `bash` calls.
  - New `memory/scratchpad.py`: thin wrapper for
    `<workspace>/.raspi/{plan.md,scratchpad.md}`. Auto-cleared on session start,
    auto-gitignored.
  - Per-session journal: at session end, append a 2-line dated entry to
    `## Notes`.
  - Drop unused `memory_meta` decay/score machinery in `memory/store.py:35`.

### Week 3 — doctor, installer, polish, tests, docs

- [ ] **Permissions and `/yolo`**
  - `RASPI_CONFIRM=write,edit,apply_patch,ast_edit,bash` (default shown).
  - `/yolo` → all-allow for session. `/strict` → re-enable.
  - Generalize `cli.py:243` bash confirm into `confirm_tool(tool_name, args)`
    reused by all write-class tools.
- [ ] **Doctor v2**
  - `--json` flag. Exit codes: 0 ok, 1 warn, 2 fail, 3 missing-deps.
  - Model warm-up benchmark uses **actual system prompt size**, not "hello".
  - Tool-call sanity probe: 5 prompts × 4 expected output shapes (native, XML,
    fenced JSON, bare JSON). Refuse to recommend a model that fails >5%.
  - Sandbox check: `bwrap --version` + `git status` smoke.
  - Promote format-extraction tests from `tests/test_core.py:18-67` into
    reusable doctor probes.
- [ ] **Hash-verified installer + `raspi --tune`**
  - `curl -fsSL .../install.sh | sh -s -- --check-hash`:
    1. Download release tarball + SHA256 sum file.
    2. Verify, extract.
    3. Install Ollama if missing.
    4. Pull `qwen2.5-coder:1.5b` and `qwen3:4b` (configurable via env).
    5. `uv tool install raspi-agent` (ARM64 wheels, ~30× faster than pipx).
    6. Symlink `~/.local/bin/raspi`.
    7. Print first-5-things panel: `raspi --doctor`, `raspi "..."`, `raspi
       --workspace ~/proj`, `/wiki`, `raspi --tune`.
  - `raspi --tune` subcommand runs existing `scripts/tune-pi.sh`.
- [ ] **Session resume + `/diff` + slash commands**
  - `raspi resume` (or `raspi resume <session-id>`) loads last session's history.
  - `/diff` slash + post-tool-sequence auto-print: `git diff --stat` of workspace
    if it's a git repo.
  - New slashes: `/plan`, `/scratchpad`, `/cheatsheet`, `/dashboard`, `/sandbox`,
    `/yolo`, `/strict`, `/diff`, `/resume`.
- [ ] **MCP-ready interface (no MCP yet)**
  - Add `source: Literal["native", "mcp"] = "native"` to `Tool` in
    `tools/registry.py`.
  - Refactor `build_tools()` so a future `from_mcp_provider(...)` can merge in
    tools at registration. **Don't ship MCP provider** — that's v0.4. Just don't
    paint into a corner.
- [ ] **Tests**
  - `tests/conftest.py`: `FakeBrain` fixture (generalize `_LoopingBrain` from
    `test_core.py:135`).
  - New: `tests/test_loop.py` (Plan-Act-Reflect, max-loops, dedupe),
    `tests/test_edit_ladder.py` (golden tests), `tests/test_compact.py`,
    JSON-repair cases, cancellation, permission gate.
  - Sandbox smoke: skip-if-not-bwrap; on Linux CI run a Debian Docker container.
- [ ] **README rewrite for v0.3**
- [ ] **Full `pytest` + `ruff` pass**

---

## Out of scope for v0.3 — deferred

| feature | why deferred |
|---|---|
| MCP provider | Pulls pydantic+anyio, doubles cold start. Ship interface only; provider in v0.4. |
| Sub-agent / `task` tool | Small-model recursion is a footgun. Wait for MCP source separation. |
| Textual TUI | Heavy dep, slow first-paint on Pi. Stay with `rich` + `prompt_toolkit`. |
| Constrained decoding | Requires logit access → Ollama HTTP doesn't expose. JSON-repair gets us 95%+. |
| `read_image` / multimodal | 4B vision model on a Pi recognizes nothing load-bearing. |
| Hailo AI HAT+ 2 path | Documented env-var stub `RASPI_NPU=hailo` raises NotImplementedError until SDK supports 3B+. |
| Voice / wake-word | Vaporware in 2026 for Pi-class. |

---

## Critical files

**Modify:**
- `pyproject.toml` — entry points, deps (tree-sitter)
- `raspi_agent/agent.py` — Plan/Act/Reflect/Compact split
- `raspi_agent/llm.py` — streaming, JSON repair, cancellation
- `raspi_agent/cli.py` — rename, slash commands, `--tune`, `resume`, permission gate
- `raspi_agent/config.py` — `RASPI_*` env vars + back-compat shim
- `raspi_agent/prompts/system.md` — Plan-Act-Reflect prompt rewrite
- `raspi_agent/prompts/consolidate.md` — add Cheatsheet extraction
- `raspi_agent/tools/registry.py` — `Tool.source`, edit-ladder tools, glob/grep
  split, todo_write
- `raspi_agent/tools/shell.py` — bwrap wrapper
- `raspi_agent/memory/wiki.py` — Cheatsheet heading
- `raspi_agent/memory/consolidate.py` — Cheatsheet extraction
- `raspi_agent/doctor.py` — `--json`, model probe, sandbox check
- `scripts/install.sh` — hash-verified, `uv tool install`
- `README.md` — full rewrite for v0.3

**Add:**
- `raspi_agent/tools/edit_ladder.py` — ast_edit + apply_patch
- `raspi_agent/memory/scratchpad.py` — plan.md, scratchpad.md
- `raspi_agent/doctor/probes.py` — model + tool-call probes
- `tests/conftest.py` — FakeBrain
- `tests/test_loop.py`, `tests/test_edit_ladder.py`, `tests/test_compact.py`,
  `tests/test_sandbox.py`

**Reuse as-is (don't touch):**
- `raspi_agent/memory/store.py` — clean SQLite, WAL, KV (drop only the unused
  `memory_meta` block)
- `raspi_agent/tools/fs.py` — `_resolve_in` workspace-escape check is correct
- `raspi_agent/tools/web.py` — private-IP guard solid
- `scripts/tune-pi.sh` — keep, wire into `raspi --tune`
- `scripts/install-dream-timer.sh` — keep
- `raspi_agent/llm.py` `_try_extract_tool_calls_from_text` and
  `_post_chat_with_fallback` — battle-hardened, just add JSON-repair around them

---

## Highest-risk pieces

1. **Tool-call reliability on `qwen2.5-coder:1.5b`.** Mitigation: doctor's
   tool-call probe is a hard install-time gate — refuse to set as default if
   >5% fail. Fall back to `qwen3:1.7b`.
2. **tree-sitter ARM64 wheels.** Mitigation: install script verifies first; if
   not available, ast_edit ships disabled. apply_patch + edit + write still
   cover edits.
3. **Reflect spiraling on 1.7B model.** Mitigation: cap reflect output 200
   tokens, force `{"keep","replan","give_up"}` 3-way choice, never free-form.
4. **bwrap + git oddities.** Mitigation: `RASPI_SANDBOX=off` escape hatch,
   doctor smoke test, ro-bind `~/.gitconfig` only.
5. **Plan-file drift** (model rewrites the user's prompt). Mitigation: pin
   user prompt verbatim at top of plan.md, refuse edits to that prefix.
6. **Streaming + tool calls interaction.** Mitigation: only stream the
   final-answer turn. Tool-call turns stay non-streaming.

---

## Verification (end-to-end on real Pi 5 8 GB)

```bash
# Fresh install path
curl -fsSL <url>/install.sh | sh -s -- --check-hash
raspi --doctor --json | jq .         # all checks green; tool-call probe passes
raspi --tune                          # governor=performance, swappiness=1

# Smoke
raspi "Make a hello.py that prints 'hi from raspi' and run it"
# expect: plan.md written, ast_edit or write used, bash runs hello.py, streaming output

# Multi-step
raspi --workspace ~/myproj "Read README, find install section, fix typos, commit"
# expect: >3 tool loops, reflect step fires once, /diff at end, sandbox active

# Resume
raspi --quit-after 1     # exits after one turn
raspi resume             # loads last session, plan.md restored

# Memory
raspi "/sleep"           # dream pass writes Cheatsheet section
raspi "/wiki"            # shows new Cheatsheet entries

# Permissions
raspi "/yolo" "..." "/strict"   # confirm gate toggles correctly
```

Local CI:
```bash
uv tool install -e ".[dev]"
pytest tests/ -v
ruff check .
```

Linux-only sandbox tests:
```bash
docker run --rm -v $PWD:/r debian:13 bash -lc "apt-get update && \
  apt-get install -y bubblewrap python3-pip && cd /r && pip install -e . && \
  pytest tests/test_sandbox.py"
```

---

## v0.4 (later)

- MCP provider on top of `Tool.source` interface
- `task` sub-agent (1 level deep, capped context)
- Hailo AI HAT+ 2 path when SDK supports 3B+ models
