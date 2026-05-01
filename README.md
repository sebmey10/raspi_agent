# rasp — on-board agent for Raspberry Pi 5

A small, persistent, Claude-Code-shaped CLI agent that runs entirely on the Pi.
Local LLM via `ollama`, file/shell/web tool use, and a single **LLM-Wiki**
markdown file (Karpathy-style) as long-term memory — no RAG, no embeddings.
A nightly `dream` pass rewrites the wiki tighter.

## What it is

- **REPL** with tool-call tracing (`rich`)
- **Tools**: `read`, `write`, `edit`, `bash` (sandboxed allowlist), `web_fetch`,
  `remember`, `forget`
- **Memory** — one canonical file at `data/memory/WIKI.md` with fixed `## H2`
  headings (`Identity`, `User`, `Active Projects`, `Preferences`, `References`,
  `Notes`, `Archive`). The full wiki is loaded into the system prompt every turn.
  - `remember(heading, note)` appends a bullet under one heading.
  - `forget(needle)` removes the first matching line.
  - Episodic SQLite log of every turn for the dream pass to read from.
- **Dream consolidation** — `/sleep` (or nightly systemd timer at 03:00) reads
  recent episodic turns, asks the model to rewrite `WIKI.md` tighter, and
  snapshots the previous version under `data/memory/_snapshots/`.
- **Tier escalation** — runs on `fast` model by default; auto-promotes to
  `reasoner` after 2 consecutive failures on the same task. Force with
  `/fast` / `/smart`, or back to `/auto`.

## Hardware target

Raspberry Pi 5, 8 GB RAM, Debian 13. Two-tier brain that respects what Pi NEON
is good at:

| tier      | model                          | resident | first turn (cold) | warm turn |
|-----------|--------------------------------|---------:|------------------:|----------:|
| fast      | `qwen2.5-coder:1.5b` (Q4_K_M)  |   ~1 GiB |              ~55s |     ~1-3s |
| reasoner  | `gemma3n-e2b-iq3xs` (IQ3_XS)   | ~2.7 GiB |             ~250s |    ~5-30s |

`qwen2.5-coder:1.5b` is the default fast tier because k-quants run fast on
Pi 5 NEON. The reasoner is `gemma3n-e2b-iq3xs`, imported via `ollama create`
from bartowski's IQ3_XS GGUF of `google/gemma-3n-E2B-it` (~2.17 GB on disk,
~2.7 GiB resident). It's only used when the agent escalates after repeated
failures, since IQ-quants are arithmetic-heavy and noticeably slower on Pi CPU.

> **Why not stock `gemma3n:e2b` from ollama's library?** That tag ships
> Q4-class weights at ~5.6 GB on disk and ~6 GiB resident — too tight on a
> Pi 5 8 GB once the agent process is running. IQ3_XS uses llama.cpp's
> importance-matrix-guided 3-bit quantization, which gets noticeably better
> perplexity than plain Q3_K_S at the same byte budget.

## Install

```bash
bash scripts/install.sh         # downloads the GGUF and runs `ollama create`
source .venv/bin/activate
rasp
```

The install script will:
1. Verify `ollama serve` is running on `127.0.0.1:11434`.
2. Download `google_gemma-3n-E2B-it-IQ3_XS.gguf` from HuggingFace
   (`bartowski/google_gemma-3n-E2B-it-GGUF`) into `data/gguf/` if missing.
3. `ollama create gemma3n-e2b-iq3xs -f data/gguf/Modelfile.gemma3n-e2b-iq3xs`
   with a Gemma chat-format `TEMPLATE` and `<start_of_turn>` / `<end_of_turn>`
   stop tokens.
4. Create the venv and `pip install -e .`.

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
| `RASP_MODEL_FAST` | `qwen2.5-coder:1.5b` | default brain |
| `RASP_MODEL_REASONER` | `gemma3n-e2b-iq3xs` | escalation brain |

## Notes on quantization

- `TurboQuant` was researched and is currently unverifiable (no paper, no repo).
  The closest practical "rotate-then-quantize" methods are
  [QuaRot](https://arxiv.org/abs/2404.00456),
  [QuIP#](https://arxiv.org/abs/2402.04396), and
  [SpinQuant](https://arxiv.org/abs/2405.16606), none of which currently target
  ARM64 + ollama.
- llama.cpp's IQ-quants (IQ2_XS, IQ3_XS, IQ4_XS) use an imatrix-guided
  k-quant scheme that is the best practical fit for a Pi today.
- If you want a smaller model still, swap the GGUF in
  `data/gguf/Modelfile.gemma3n-e2b-iq3xs` for `IQ2_XS` (~1.9 GB, lower quality)
  or move up to `IQ4_XS` (~2.6 GB) if you have RAM headroom.
