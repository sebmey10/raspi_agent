# raspi — on-board Claude-Code-shaped agent for Raspberry Pi 5

A small, persistent, **fully local** coding/IT agent. Type `raspi` on a Pi 5
and you get a Plan-Act-Reflect loop with streaming output, an AST-aware edit
ladder, a bubblewrap-sandboxed shell, an LLM-Wiki long-term memory that
rewrites itself nightly, and tier-escalating local models on Ollama.

> v0.3.0 — major rewrite. The `rasp` command and `RASP_*` env vars still work
> for one release; both will be removed in v0.4. New name everywhere is
> `raspi` / `RASPI_*`.

## What is it

- **Plan → Act → Reflect** loop. Multi-step tasks open with a `todo_write`
  that pins the user's prompt and writes `<workspace>/.raspi/plan.md`. Every
  4 tool loops the model runs a one-word reflect (`keep` / `replan` /
  `give_up`). Default `MAX_TOOL_LOOPS=12`, smarter dedupe (normalized args).
- **Streaming output**. Final-answer turns stream tokens to stdout via
  `rich`. Ctrl-C cancels both the in-flight LLM stream and any tool
  subprocess.
- **Context compaction**. When transcript hits 80% of the history budget,
  the fast tier summarizes the oldest half into one synthetic turn — long
  Pi sessions stop collapsing.
- **Edit ladder**. The model picks the right tool: `ast_edit` (Python/JS via
  tree-sitter, parse-checked) → `apply_patch` (unified diff via stdlib
  `difflib`) → `edit` (single literal replace) → `write` (full overwrite,
  last resort).
- **Bubblewrap sandbox**. If `bwrap` is on `PATH`, every shell call runs
  with `--ro-bind /`, `--bind <workspace>`, `--unshare-net` (network is
  re-shared for `curl`/`wget`/`git`). Auto-detected; opt out with
  `RASPI_SANDBOX=off`.
- **LLM-Wiki memory**. One canonical `WIKI.md` with fixed H2 headings
  (`Identity`, `User`, `Active Projects`, `Preferences`, `References`,
  `Cheatsheet`, `Notes`, `Archive`). Loaded into the system prompt every
  turn; nightly `dream` consolidates it. New `Cheatsheet` heading collects
  reusable shell snippets.
- **Per-session journal**. Every session ends with a 2-line dated note in
  `## Notes` so persistence is visible.
- **`/yolo` and write-class permission gate**. Default-allow read tools;
  prompt before `write`/`edit`/`apply_patch`/`ast_edit`/`bash`. `/yolo`
  skips prompts for the session, `/strict` re-enables.
- **Doctor v2**. `raspi --doctor [--json]` probes Ollama, models, Pi
  thermals, throttle bits, governor, swappiness, sandbox, tree-sitter,
  prompt budgets, threads. Exit codes: 0 ok, 1 warn, 2 fail.
- **Session resume**. `raspi resume` (or `/resume`) reloads the most recent
  session's history into the new agent.
- **MCP-ready interface**. `Tool.source` field is in place; v0.4 ships an
  MCP provider on top of the same registry without touching the loop.

## Install

```bash
git clone https://github.com/<you>/raspi_agent.git
cd raspi_agent
bash scripts/install.sh        # uv tool install if available, else pipx, else .venv
raspi --tune                   # cpu governor=performance, swappiness=1
raspi --doctor                 # verify sandbox, models, Pi health
raspi                          # interactive REPL
```

The installer:
1. Installs Ollama if missing.
2. Pulls `qwen2.5-coder:1.5b` (fast) and `qwen3:4b` (reasoner) — overridable
   via `RASPI_MODEL_FAST` / `RASPI_MODEL_REASONER`.
3. Installs `raspi-agent` via `uv tool install` (preferred), `pipx`, or a
   local `.venv`. Force the venv path with `bash scripts/install.sh --venv`.
4. Creates `~/raspi_ws` (workspace) and `<repo>/data/memory/WIKI.md`.

Optional:
```bash
bash scripts/install-dream-timer.sh   # systemd user timer at 03:00
pip install raspi-agent[ast]          # tree-sitter wheels for ast_edit
```

## Default model lineup (Pi 5 8 GB, Q4_K_M)

| tier      | model                       | resident | tok/s    | role                                |
|-----------|-----------------------------|---------:|---------:|-------------------------------------|
| fast      | `qwen2.5-coder:1.5b`        | ~1.2 GiB | ~12-15   | coding, tool calls, glue            |
| reasoner  | `qwen3:4b`                  | ~3.5 GiB | ~5-6     | escalation, `/think` mode           |

Tier escalates on 2 consecutive plan-step failures, and auto-downgrades when
the run stabilizes. Force with `/fast`, `/smart`, or `/auto`.

`raspi --doctor --json` will tell you whether your installed Ollama has both
tags. If not, it falls back to whatever you have and warns.

## Slash commands

| command           | what                                               |
|-------------------|----------------------------------------------------|
| `/wiki`           | print the full WIKI.md                             |
| `/plan`           | print the current `.raspi/plan.md`                 |
| `/scratchpad`     | print the current `.raspi/scratchpad.md`           |
| `/cheatsheet`     | print the wiki's `## Cheatsheet` section           |
| `/diff`           | `git diff --stat` of the workspace                 |
| `/dashboard`      | session totals (turns, tokens, sandbox, etc.)      |
| `/sleep`          | run consolidation (dream pass) now                 |
| `/dreams`         | show last consolidation summary                    |
| `/forget <needle>`| remove first matching line                         |
| `/fast`           | force fast tier                                    |
| `/smart`          | force reasoner tier                                |
| `/auto`           | auto tier (fast + escalate on failure)             |
| `/yolo`           | disable tool-confirmation prompts for the session  |
| `/strict`         | re-enable tool-confirmation prompts                |
| `/sandbox`        | print sandbox state (bwrap on/off + mode)          |
| `/resume [sid]`   | reload most recent (or named) session into history |
| `/quit`           | exit (saves session, runs journal entry)           |

## Layout

```
data/
  db.sqlite              # episodic turns, dreams, kv
  memory/
    WIKI.md              # canonical long-term memory
    _snapshots/          # pre-dream backups
  sessions/              # (reserved)

<workspace>/.raspi/      # ephemeral per-session state
  plan.md                # current TODO list (gitignored)
  scratchpad.md          # agent's scratch notes (gitignored)
  .gitignore
```

## Env knobs

All `RASPI_*` vars also accept the legacy `RASP_*` name for one release.

| var                              | default                  | what                                              |
|----------------------------------|--------------------------|---------------------------------------------------|
| `RASPI_DATA_DIR`                 | `<repo>/data`            | where memory + db live                            |
| `RASPI_WORKSPACE`                | `~/raspi_ws`             | where the agent reads/writes files                |
| `OLLAMA_URL`                     | `http://127.0.0.1:11434` | ollama daemon                                     |
| `RASPI_MODEL_FAST`               | `qwen2.5-coder:1.5b`     | default fast tier                                 |
| `RASPI_MODEL_REASONER`           | `qwen3:4b`               | default reasoner                                  |
| `RASPI_NATIVE_TOOLS`             | `auto`                   | `auto`, `on`, `off` for Ollama tool calls         |
| `RASPI_SANDBOX`                  | `auto`                   | `auto` (use bwrap if present), `on`, `off`        |
| `RASPI_CONFIRM`                  | `write,edit,apply_patch,ast_edit,bash` | tools that prompt before running        |
| `RASPI_MAX_TOOL_LOOPS`           | `12`                     | hard cap on Act loops per turn                    |
| `RASPI_REFLECT_EVERY`            | `4`                      | reflect checkpoint interval (0 disables)          |
| `RASPI_COMPACT_THRESHOLD`        | `0.8`                    | history-budget ratio that triggers compaction     |
| `RASPI_FAIL_STREAK_TO_ESCALATE`  | `2`                      | failures before tier escalates                    |
| `RASPI_THINK_FAST`               | `false`                  | qwen `/think` mode for fast tier                  |
| `RASPI_THINK_REASONER`           | `auto`                   | `false`/`true`/`low`/`medium`/`high`/`auto`       |
| `RASPI_CTX_FAST`                 | `2048`                   | fast-tier num_ctx                                 |
| `RASPI_CTX_REASONER`             | `4096`                   | reasoner-tier num_ctx                             |
| `RASPI_HISTORY_PROMPT_CHARS`     | `7000`                   | recent-transcript budget                          |
| `RASPI_WIKI_PROMPT_CHARS`        | `9000`                   | wiki memory budget                                |
| `RASPI_PLAN_PROMPT_CHARS`        | `1500`                   | plan-file injection budget                        |
| `RASPI_TOOL_RESULT_CHARS`        | `5000`                   | clip per-tool result before history               |
| `RASPI_DREAM_MAX_TURNS`          | `256`                    | max turns per nightly consolidation               |
| `RASPI_DREAM_TRANSCRIPT_CHARS`   | `24000`                  | dream transcript budget                           |
| `RASPI_WEB_ALLOW_PRIVATE`        | `false`                  | allow `web_fetch` to reach localhost / RFC1918    |
| `RASPI_AUTO_DREAM_ON_EXIT`       | `false`                  | run consolidation on exit                         |
| `RASPI_NPU`                      | `off`                    | `off` or `hailo` (Hailo path is a v0.4 stub)      |

## Tool surface (v0.3)

| tool         | role                                                                |
|--------------|---------------------------------------------------------------------|
| `read`       | paged file read, dir listing                                        |
| `glob`       | workspace listing by glob (`list_files` is a deprecated alias)      |
| `grep`       | ripgrep-backed literal search (`search` is a deprecated alias)      |
| `write`      | full-file write (last resort)                                       |
| `edit`       | unique-match single-replace                                         |
| `apply_patch`| unified-diff hunks via stdlib `difflib`                             |
| `ast_edit`   | tree-sitter Python/JS named-def replace, parse-checked              |
| `bash`       | allowlisted shell, bwrap-sandboxed when available                   |
| `web_fetch`  | HTTPS fetch with private-IP guard                                   |
| `todo_write` | write the per-session plan file                                     |
| `remember`   | append a bullet under a wiki H2 heading                             |
| `forget`     | remove first wiki line containing a needle                          |

## Notes on quantization (still relevant)

- Q4_K_M is the production sweet spot on Pi 5 NEON. No 2026 ARM-specific
  quant has decisively beaten it on small models.
- For models <=4B, IQ3 quants degrade tool-call reliability noticeably.
  Stay on Q4_K_M unless RAM is genuinely tight.

## Acknowledgements

- LLM-Wiki memory pattern after [Karpathy](https://twitter.com/karpathy)'s
  "single-file memory" sketches.
- Tool-calling rankings cross-checked against the Berkeley Function-Calling
  Leaderboard v4.
- Pi 5 inference baselines from [Stratosphere Lab](https://www.stratosphereips.org/blog/2025/6/5/how-well-do-llms-perform-on-a-raspberry-pi-5)
  and the SBC inference paper (arxiv:2511.07425).
- Sandbox model after Claude Code's bwrap setup on Linux.
