# rasp — on-board agent for Raspberry Pi 5

A small, persistent, Claude-Code-shaped CLI agent that runs entirely on the Pi.
Local LLM via `ollama`, file/shell/web tool use, and a single **LLM-Wiki**
markdown file (Karpathy-style) as long-term memory — no RAG, no embeddings.
A nightly `dream` pass rewrites the wiki tighter.

## What it is

- **REPL + one-shot CLI** with tool-call tracing (`rich`)
- **Tools**: `list_files`, paged `read`, `write`, `edit`, `search`, `bash`
  (validated allowlist + approval path), `web_fetch`, `remember`, `forget`
- **Memory** — one canonical file at `data/memory/WIKI.md` with fixed `## H2`
  headings (`Identity`, `User`, `Active Projects`, `Preferences`, `References`,
  `Notes`, `Archive`). The active wiki budget is loaded into the system prompt
  every turn.
  - `remember(heading, note)` appends a bullet under one heading.
  - `forget(needle)` removes the first matching line.
  - Episodic SQLite log of every turn for the dream pass to read from.
- **Dream consolidation** — `/sleep` (or nightly systemd timer at 03:00) reads
  recent episodic turns, asks the model to rewrite `WIKI.md` tighter, and
  snapshots the previous version under `data/memory/_snapshots/`.
- **Prompt budgeting** — only recent turns and clipped tool results are sent to
  Ollama; the SQLite transcript still keeps the full session.
- **Tier escalation** — runs on `fast` model by default; auto-promotes to
  `reasoner` after 2 consecutive failures on the same task. Force with
  `/fast` / `/smart`, or back to `/auto`.
- **Native tools when useful** — defaults to `RASP_NATIVE_TOOLS=auto`, so Qwen3
  can use Ollama's tool-call field while older/custom models keep the XML
  fallback.

## Hardware target

Raspberry Pi 5, 8 GB RAM, Debian 13. Two Qwen3 Q4_K_M tiers — same tool-call
format, no IQ-quant arithmetic penalty on ARM:

| tier      | model         | resident | first turn (cold) | warm turn |
|-----------|---------------|---------:|------------------:|----------:|
| fast      | `qwen3:1.7b`  | ~1.4 GiB |              ~60s |     ~1-4s |
| reasoner  | `qwen3:4b`    | ~3.5 GiB |             ~120s |    ~5-30s |

`qwen3:1.7b` ranks #1 on the public small-model tool-calling benchmark and has
a 1.000 restraint score (correctly declines to call tools when not needed),
which is what kills most small-agent loops. `qwen3:4b` shares the same chat
template and tool format and supports `/think` mode for harder turns. Both run
~10-13 and ~5-6 tok/s respectively on a Pi 5 with `performance` governor.

Avoid IQ3 quants for agentic use on ARM CPU: the arithmetic is heavier than
Q4_K_M, and tool-call reliability degrades noticeably below 4B at IQ3.

## Install

```bash
bash scripts/install.sh         # pulls qwen3:1.7b and qwen3:4b
bash scripts/tune-pi.sh         # Pi 5 perf tuning (governor, swappiness, ollama unit)
source .venv/bin/activate
rasp --doctor                   # verify
rasp
```

The install script will:
1. Verify `ollama serve` is running on `127.0.0.1:11434`.
2. Pull `qwen3:1.7b` (or `RASP_MODEL_FAST`).
3. Pull `qwen3:4b` (or `RASP_MODEL_REASONER`).
4. Create the venv and `pip install -e .`.

The tune script (`scripts/tune-pi.sh`) sets `cpufreq=performance`,
`vm.swappiness=1`, and writes a systemd drop-in for the `ollama.service` unit
with `LimitMEMLOCK=infinity`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_NUM_PARALLEL=1`,
`OLLAMA_MAX_LOADED_MODELS=1`. Idempotent; pass `--revert` to undo.

> **Upgrading from older versions:** the reasoner default changed from
> `gemma3n-e2b-iq3xs` (IQ3_XS, slower on ARM) to `qwen3:4b`. If you want the
> old default, set `RASP_MODEL_REASONER=gemma3n-e2b-iq3xs` and pull/create
> the model yourself.

Useful first checks:

```bash
rasp --doctor
rasp "Say hi in one short sentence."
rasp --workspace ~/my_project "Find the README and summarize it."
```

Optional: nightly dream consolidation timer

```bash
bash scripts/install-dream-timer.sh
```

## Slash commands

| command | what |
|---------|------|
| `/wiki` | print the full WIKI.md |
| `/forget <needle>` | remove first matching line |
| `/sleep` | run consolidation now |
| `/dreams` | show last consolidation summary |
| `/fast` | force fast tier |
| `/smart` | force reasoner tier |
| `/auto` | auto tier (fast + escalate on failure) |
| `/ws <path>` | show / hint workspace path |
| `/quit` | exit (saves session) |

## Layout

```
data/
  db.sqlite              # episodic turns, dream log
  gguf/                  # downloaded GGUF + Modelfile
  memory/
    WIKI.md              # the one canonical memory file
    _snapshots/          # pre-dream backups
  sessions/              # (reserved for jsonl exports)
```

## Env knobs

| var | default | what |
|-----|---------|------|
| `RASP_DATA_DIR` | `<repo>/data` | where memory + db live |
| `RASP_WORKSPACE` | `~/rasp_agent_ws` | where the agent reads/writes files |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | ollama daemon |
| `RASP_MODEL_FAST` | `qwen3:1.7b` | default brain |
| `RASP_MODEL_REASONER` | `qwen3:4b` | escalation brain |
| `RASP_NATIVE_TOOLS` | `auto` | `auto`, `on`, or `off` for Ollama tool calls |
| `RASP_THINK_FAST` | `false` | disable Qwen3 thinking for fast turns |
| `RASP_CTX_FAST` | `2048` | fast-tier context window |
| `RASP_HISTORY_PROMPT_CHARS` | `7000` | recent transcript budget |
| `RASP_WIKI_PROMPT_CHARS` | `9000` | wiki memory budget |
| `RASP_DREAM_MAX_TURNS` | `256` | max transcript turns consumed per dream pass |
| `RASP_DREAM_TRANSCRIPT_CHARS` | `24000` | dream transcript prompt budget |
| `RASP_WEB_ALLOW_PRIVATE` | `false` | allow `web_fetch` to reach localhost/private IPs |
| `RASP_AUTO_DREAM_ON_EXIT` | `false` | run consolidation when the CLI exits |

## Notes on quantization

- Q4_K_M is the production sweet spot on Pi 5 NEON. ARM dotprod accelerates the
  4-bit matrix kernels; IQ3/IQ2 schemes win in bytes but lose ~15-30% on
  generation throughput because the imatrix-style decode is arithmetic-heavy
  on a CPU.
- For models <=4B parameters, IQ3 quantization noticeably degrades
  tool-call reliability — small models have less precision headroom. Stay on
  Q4_K_M unless RAM is genuinely tight.
- If you have ~5GB headroom and want quality over speed, try `qwen3:4b` Q5_K_M
  via `ollama pull qwen3:4b-q5_K_M` (or whatever tag the registry exposes).

## Acknowledgements

- Tool-calling rankings drawn from
  [MikeVeerman/tool-calling-benchmark](https://github.com/MikeVeerman/tool-calling-benchmark).
- Pi 5 inference numbers cross-checked against
  [Stratosphere Lab's Pi 5 LLM benchmarks](https://www.stratosphereips.org/blog/2025/6/5/how-well-do-llms-perform-on-a-raspberry-pi-5)
  and the
  [SBC inference evaluation paper (arxiv:2511.07425)](https://arxiv.org/html/2511.07425v1).
