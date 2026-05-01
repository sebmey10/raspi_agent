from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .agent import Agent, build_agent
from .config import CONFIG, Config
from .doctor import run_checks
from .llm import TIER_FAST, TIER_REASONER
from .memory.consolidate import consolidate

console = Console()


def banner(cfg: Config) -> None:
    console.print(Panel.fit(
        f"[bold cyan]rasp[/bold cyan] — on-board agent\n"
        f"fast: [green]{cfg.model_fast}[/green]   reasoner: [yellow]{cfg.model_reasoner}[/yellow]\n"
        f"workspace: [magenta]{cfg.workspace}[/magenta]\n"
        f"data:      [dim]{cfg.data_dir}[/dim]\n"
        f"native tools: [cyan]{cfg.native_tools}[/cyan]   think_fast: [cyan]{cfg.think_fast!r}[/cyan]\n"
        f"slash: /wiki /forget <needle> /sleep /dreams /fast /smart /auto /ws <p> /quit",
        border_style="cyan",
    ))


def render_tool_call(name: str, args: dict, result: str) -> None:
    arg_text = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
    is_err = "<error" in result
    color = "red" if is_err else "blue"
    console.print(f"[{color}]» {name}({arg_text})[/{color}]")
    snippet = result.strip()
    if len(snippet) > 600:
        snippet = snippet[:600] + " …"
    console.print(f"[dim]{snippet}[/dim]")


def _short(v) -> str:
    s = str(v)
    return s if len(s) <= 80 else s[:77] + "…"


def _fmt_age(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta/60)}m"
    if delta < 86400:
        return f"{int(delta/3600)}h"
    return f"{int(delta/86400)}d"


def cmd_wiki(agent: Agent) -> None:
    text = agent.d.wiki.text()
    console.print(Markdown(text))


def cmd_forget(agent: Agent, needle: str) -> None:
    if not needle:
        console.print("[red]usage: /forget <needle>[/red]")
        return
    res = agent.d.wiki.forget(needle)
    if res.startswith("<error"):
        console.print(f"[red]{res}[/red]")
    else:
        console.print(f"[green]{res}[/green]")


def cmd_sleep(agent: Agent) -> None:
    console.print("[yellow]sleeping… (running consolidation)[/yellow]")
    res = consolidate(agent.d.cfg)
    if res.get("skipped"):
        console.print(f"[dim]skipped: {res['reason']}[/dim]")
        return
    console.print(Panel(
        f"{res['summary']}\n[dim]turns_consumed={res['turns']}  "
        f"written={res['written']}[/dim]",
        title="dream", border_style="yellow",
    ))


def cmd_dreams(agent: Agent) -> None:
    last = agent.d.store.last_dream()
    if not last:
        console.print("[dim]no dreams yet[/dim]")
        return
    age = _fmt_age(last["ran_at"])
    console.print(Panel(
        f"{last['summary']}\n[dim]turns_consumed={last['turns_consumed']}  "
        f"written={last['memories_written']}  age={age}[/dim]",
        title="last dream", border_style="yellow",
    ))


def cmd_ws(agent: Agent, path: str) -> None:
    if not path:
        console.print(f"workspace: {agent.d.cfg.workspace}")
        return
    console.print(
        f"[yellow]workspace is locked for this session[/yellow]\n"
        f"[dim]restart with RASP_WORKSPACE={path} rasp[/dim]"
    )


def cmd_doctor(cfg: Config) -> int:
    checks = run_checks(cfg)
    table = Table(title="rasp doctor", show_header=True, header_style="bold cyan")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    for check in checks:
        color = {"ok": "green", "warn": "yellow", "fail": "red"}.get(check.status, "white")
        table.add_row(check.name, f"[{color}]{check.status}[/{color}]", check.detail)
    console.print(table)
    return 1 if any(c.status == "fail" for c in checks) else 0


def handle_slash(agent: Agent, line: str) -> bool:
    parts = line.strip().split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "/quit":
        return False
    if cmd == "/wiki":
        cmd_wiki(agent)
    elif cmd == "/forget":
        cmd_forget(agent, arg.strip())
    elif cmd == "/sleep":
        cmd_sleep(agent)
    elif cmd == "/dreams":
        cmd_dreams(agent)
    elif cmd == "/fast":
        agent.forced_tier = TIER_FAST
        console.print("[green]forced fast tier[/green]")
    elif cmd == "/smart":
        agent.forced_tier = TIER_REASONER
        console.print("[yellow]forced reasoner tier[/yellow]")
    elif cmd == "/auto":
        agent.forced_tier = None
        console.print("[cyan]auto-tier (fast with escalation)[/cyan]")
    elif cmd == "/ws":
        cmd_ws(agent, arg.strip())
    else:
        console.print(f"[red]unknown slash command: {cmd}[/red]")
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="rasp: on-board Raspberry Pi agent")
    parser.add_argument("prompt", nargs="*", help="run one prompt and exit")
    parser.add_argument("-p", "--prompt", dest="prompt_opt", help="run one prompt and exit")
    parser.add_argument("--doctor", action="store_true", help="check Ollama, models, workspace, and Pi health")
    parser.add_argument("--no-warm", action="store_true", help="skip startup model warm")
    parser.add_argument("--tier", choices=("auto", "fast", "reasoner"), default="auto", help="initial model tier")
    parser.add_argument("--workspace", type=Path, help="workspace directory for this run")
    parser.add_argument("--data-dir", type=Path, help="data directory for this run")
    parser.add_argument("--fast-model", help="override fast model")
    parser.add_argument("--reasoner-model", help="override reasoner model")
    parser.add_argument("--native-tools", choices=("auto", "on", "off"), help="Ollama native tool-call mode")
    return parser.parse_args(argv)


def _cfg_from_args(args: argparse.Namespace) -> Config:
    updates = {}
    if args.workspace:
        updates["workspace"] = args.workspace.expanduser()
    if args.data_dir:
        updates["data_dir"] = args.data_dir.expanduser()
    if args.fast_model:
        updates["model_fast"] = args.fast_model
    if args.reasoner_model:
        updates["model_reasoner"] = args.reasoner_model
    if args.native_tools:
        updates["native_tools"] = args.native_tools
    return replace(CONFIG, **updates) if updates else CONFIG


def _set_tier(agent: Agent, tier: str) -> None:
    if tier == "fast":
        agent.forced_tier = TIER_FAST
    elif tier == "reasoner":
        agent.forced_tier = TIER_REASONER
    else:
        agent.forced_tier = None


def _stats_summary(agent: Agent) -> str:
    if not agent.last_turn_stats:
        return ""
    last = agent.last_turn_stats[-1]
    bits = [f"[{last['tier']}]", str(last.get("model") or "")]
    if last.get("load_s"):
        bits.append(f"load={last['load_s']:.1f}s")
    if last.get("prompt_tokens"):
        bits.append(f"prompt={last['prompt_tokens']} @ {last['prompt_tok_s']:.1f} tok/s")
    if last.get("eval_tokens"):
        bits.append(f"gen={last['eval_tokens']} @ {last['eval_tok_s']:.1f} tok/s")
    return " ".join(bits)


def _run_one(agent: Agent, prompt: str) -> int:
    t0 = time.time()
    answer = agent.turn(prompt)
    elapsed = time.time() - t0
    if answer.strip():
        console.print(Markdown(answer))
    stats = _stats_summary(agent)
    console.print(f"[dim]{stats} {elapsed:.1f}s[/dim]")
    return 0


def _finish(agent: Agent, deps, cfg: Config) -> None:
    deps.store.end_session(agent.session_id)
    if cfg.auto_dream_on_exit:
        try:
            res = consolidate(cfg)
            if not res.get("skipped"):
                console.print(f"[dim]auto-dream: {res['summary']}[/dim]")
        except Exception as e:
            console.print(f"[yellow]auto-dream failed: {e}[/yellow]")
    deps.brain.client.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = _cfg_from_args(args)

    if args.doctor:
        return cmd_doctor(cfg)

    banner(cfg)
    agent, deps = build_agent(cfg, on_tool_call=render_tool_call)
    _set_tier(agent, args.tier)
    if not args.no_warm:
        deps.brain.warm(agent._pick_tier())
        warm_note = "model warmed"
    else:
        warm_note = "warm skipped"
    console.print(f"[dim]session {agent.session_id} started — {warm_note}[/dim]\n")

    prompt = args.prompt_opt or (" ".join(args.prompt).strip() if args.prompt else "")
    if prompt:
        try:
            return _run_one(agent, prompt)
        finally:
            _finish(agent, deps, cfg)

    try:
        while True:
            try:
                line = console.input("[bold green]›[/bold green] ").rstrip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not handle_slash(agent, line):
                    break
                continue
            t0 = time.time()
            try:
                answer = agent.turn(line)
            except KeyboardInterrupt:
                console.print("[yellow]…interrupted[/yellow]")
                continue
            elapsed = time.time() - t0
            if answer.strip():
                console.print(Markdown(answer))
            stats = _stats_summary(agent) or f"[{agent._pick_tier()}]"
            console.print(f"[dim]{stats} {elapsed:.1f}s[/dim]\n")
    finally:
        _finish(agent, deps, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
