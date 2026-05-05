from __future__ import annotations

import argparse
import json as _json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from .agent import Agent, build_agent
from .config import CONFIG, Config
from .doctor import run_checks
from .llm import TIER_FAST, TIER_REASONER
from .memory.consolidate import consolidate

console = Console()


# ---- banners and helpers ----------------------------------------------------


def banner(cfg: Config, sandbox_active: bool) -> None:
    sandbox_label = "[green]on[/green]" if sandbox_active else "[dim]off[/dim]"
    console.print(Panel.fit(
        f"[bold cyan]raspi[/bold cyan] - on-board agent\n"
        f"fast: [green]{cfg.model_fast}[/green]   reasoner: [yellow]{cfg.model_reasoner}[/yellow]\n"
        f"workspace: [magenta]{cfg.workspace}[/magenta]\n"
        f"data:      [dim]{cfg.data_dir}[/dim]\n"
        f"native tools: [cyan]{cfg.native_tools}[/cyan]   sandbox: {sandbox_label}\n"
        f"slash: /wiki /plan /scratchpad /cheatsheet /diff /sleep /dreams\n"
        f"       /fast /smart /auto /yolo /strict /sandbox /dashboard /resume /quit",
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


# ---- slash commands ---------------------------------------------------------


def cmd_wiki(agent: Agent) -> None:
    console.print(Markdown(agent.d.wiki.text()))


def cmd_plan(agent: Agent) -> None:
    if not agent.d.scratch.plan_path.exists():
        console.print("[dim]no active plan[/dim]")
        return
    console.print(Markdown(agent.d.scratch.plan_path.read_text(encoding="utf-8")))


def cmd_scratchpad(agent: Agent) -> None:
    text = agent.d.scratch.read_scratch()
    if not text.strip():
        console.print("[dim]scratchpad is empty[/dim]")
        return
    console.print(Markdown(text))


def cmd_cheatsheet(agent: Agent) -> None:
    section = agent.d.wiki.find("Cheatsheet")
    body = (section.body if section else "").strip()
    if not body or body.startswith("("):
        console.print("[dim]no cheatsheet entries yet (build them via /sleep)[/dim]")
        return
    console.print(Markdown(f"## Cheatsheet\n\n{body}"))


def cmd_forget(agent: Agent, needle: str) -> None:
    if not needle:
        console.print("[red]usage: /forget <needle>[/red]")
        return
    res = agent.d.wiki.forget(needle)
    color = "red" if res.startswith("<error") else "green"
    console.print(f"[{color}]{res}[/{color}]")


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


def cmd_diff(agent: Agent) -> None:
    ws = agent.d.cfg.workspace
    if not (ws / ".git").exists():
        console.print("[dim]workspace is not a git repo[/dim]")
        return
    try:
        proc = subprocess.run(
            ["git", "-C", str(ws), "diff", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        console.print(f"[red]git diff failed: {e}[/red]")
        return
    out = (proc.stdout or "").strip()
    console.print(Panel(out or "[dim]no changes[/dim]",
                        title="git diff --stat", border_style="cyan"))


def cmd_dashboard(agent: Agent) -> None:
    s = agent.session_stats
    table = Table(title=f"session {agent.session_id}", show_header=False)
    table.add_row("turns", str(s.get("turns", 0)))
    table.add_row("tool calls", str(s.get("tool_calls", 0)))
    table.add_row("compactions", str(s.get("compactions", 0)))
    table.add_row("prompt tokens", str(s.get("prompt_tokens", 0)))
    table.add_row("eval tokens", str(s.get("eval_tokens", 0)))
    table.add_row("tier", agent._pick_tier())
    table.add_row("sandbox", "on" if agent.d.shell.sandbox_active() else "off")
    confirm_set = ", ".join(agent.d.cfg.confirm_tools) or "(none)"
    table.add_row("confirm-on", confirm_set)
    console.print(table)


def cmd_resume(agent: Agent, sid: str | None) -> None:
    target = sid or _last_completed_session_id(agent)
    if not target:
        console.print("[dim]no prior session found[/dim]")
        return
    if target == agent.session_id:
        console.print("[yellow]cannot resume the current session[/yellow]")
        return
    rows = agent.d.store.recent_turns(target, limit=64)
    if not rows:
        console.print(f"[red]no turns found for session {target}[/red]")
        return
    for r in rows:
        if r["role"] == "tool":
            agent.history.append({
                "role": "tool",
                "name": r.get("tool_name") or "tool",
                "content": r.get("tool_result") or r.get("content") or "",
            })
        else:
            agent.history.append({"role": r["role"], "content": r.get("content") or ""})
    console.print(f"[green]resumed {len(rows)} turns from session {target}[/green]")


def _last_completed_session_id(agent: Agent) -> str | None:
    try:
        with agent.d.store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT id FROM sessions WHERE id != ? AND ended_at IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (agent.session_id,),
            ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


def cmd_doctor(cfg: Config, json_out: bool) -> int:
    checks = run_checks(cfg)
    if json_out:
        payload = [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks]
        print(_json.dumps(payload, indent=2))
    else:
        table = Table(title="raspi doctor", show_header=True, header_style="bold cyan")
        table.add_column("check")
        table.add_column("status")
        table.add_column("detail")
        for check in checks:
            color = {"ok": "green", "warn": "yellow", "fail": "red"}.get(check.status, "white")
            table.add_row(check.name, f"[{color}]{check.status}[/{color}]", check.detail)
        console.print(table)
    if any(c.status == "fail" for c in checks):
        return 2
    if any(c.status == "warn" for c in checks):
        return 1
    return 0


def cmd_tune() -> int:
    here = Path(__file__).resolve().parent.parent
    script = here / "scripts" / "tune-pi.sh"
    if not script.exists():
        console.print(f"[red]tune script not found at {script}[/red]")
        return 2
    console.print(f"[dim]running {script}…[/dim]")
    return subprocess.call(["bash", str(script)])


def handle_slash(agent: Agent, line: str, session_state: dict) -> bool:
    parts = line.strip().split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "/quit":
        return False
    if cmd == "/wiki":
        cmd_wiki(agent)
    elif cmd == "/plan":
        cmd_plan(agent)
    elif cmd == "/scratchpad":
        cmd_scratchpad(agent)
    elif cmd == "/cheatsheet":
        cmd_cheatsheet(agent)
    elif cmd == "/diff":
        cmd_diff(agent)
    elif cmd == "/dashboard":
        cmd_dashboard(agent)
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
    elif cmd == "/yolo":
        session_state["yolo"] = True
        console.print("[red]/yolo: tool confirmations disabled for this session[/red]")
    elif cmd == "/strict":
        session_state["yolo"] = False
        console.print("[green]/strict: tool confirmations re-enabled[/green]")
    elif cmd == "/sandbox":
        active = agent.d.shell.sandbox_active()
        console.print(f"sandbox: {'on (bwrap)' if active else 'off'}; mode={agent.d.cfg.sandbox}")
    elif cmd == "/resume":
        cmd_resume(agent, arg.strip() or None)
    elif cmd == "/ws":
        if arg.strip():
            console.print("[yellow]workspace is locked for this session[/yellow]\n"
                          f"[dim]restart with RASPI_WORKSPACE={arg.strip()} raspi[/dim]")
        else:
            console.print(f"workspace: {agent.d.cfg.workspace}")
    else:
        console.print(f"[red]unknown slash command: {cmd}[/red]")
    return True


# ---- argparse / config wiring -----------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="raspi", description="raspi: on-board Pi agent")
    parser.add_argument("prompt", nargs="*", help="run one prompt and exit")
    parser.add_argument("-p", "--prompt", dest="prompt_opt", help="run one prompt and exit")
    parser.add_argument("--doctor", action="store_true", help="check Ollama, models, workspace, Pi health")
    parser.add_argument("--json", action="store_true", help="machine-readable output (with --doctor)")
    parser.add_argument("--tune", action="store_true", help="run the Pi 5 inference tuning script")
    parser.add_argument("--no-warm", action="store_true", help="skip startup model warm")
    parser.add_argument("--tier", choices=("auto", "fast", "reasoner"), default="auto", help="initial model tier")
    parser.add_argument("--workspace", type=Path, help="workspace directory for this run")
    parser.add_argument("--data-dir", type=Path, help="data directory for this run")
    parser.add_argument("--fast-model", help="override fast model")
    parser.add_argument("--reasoner-model", help="override reasoner model")
    parser.add_argument("--native-tools", choices=("auto", "on", "off"), help="Ollama native tool-call mode")
    parser.add_argument("--sandbox", choices=("auto", "on", "off"), help="bubblewrap shell sandbox mode")
    parser.add_argument("--resume", nargs="?", const="", help="resume the most recent (or named) session")
    parser.add_argument("--no-stream", action="store_true", help="disable streaming output for final answers")
    return parser.parse_args(argv)


def _cfg_from_args(args: argparse.Namespace) -> Config:
    updates: dict = {}
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
    if args.sandbox:
        updates["sandbox"] = args.sandbox
    return replace(CONFIG, **updates) if updates else CONFIG


def _set_tier(agent: Agent, tier: str) -> None:
    if tier == "fast":
        agent.forced_tier = TIER_FAST
    elif tier == "reasoner":
        agent.forced_tier = TIER_REASONER
    else:
        agent.forced_tier = None


def _stats_line(agent: Agent) -> str:
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


def _session_line(agent: Agent) -> str:
    s = agent.session_stats
    total = s.get("prompt_tokens", 0) + s.get("eval_tokens", 0)
    if not total:
        return ""
    return (f"[session: {total // 1000}k tok | {s.get('turns', 0)} turns | "
            f"{s.get('tool_calls', 0)} tools]")


def _wrap_confirm_gate(agent: Agent, cfg: Config, session_state: dict) -> None:
    """Wrap write-class tool handlers so they prompt the user before running.
    Read-only tools (read, glob, grep, list_files, web_fetch) are never gated."""
    gate_set = {n.strip() for n in cfg.confirm_tools if n.strip()}
    if not gate_set:
        return
    for name in list(agent.d.tools.keys()):
        if name not in gate_set:
            continue
        tool = agent.d.tools[name]
        original = tool.handler

        def gated(args: dict, _orig=original, _name=name):
            if session_state.get("yolo"):
                return _orig(args)
            preview = _short_args(args)
            console.print(Panel(
                f"[bold]Tool wants to run:[/bold] [cyan]{_name}[/cyan]\n{preview}",
                title="approve?", border_style="yellow",
            ))
            if not Confirm.ask(f"Run {_name}?", default=False):
                return f"<error>{_name} denied by user</error>"
            return _orig(args)

        tool.handler = gated


def _short_args(args: dict) -> str:
    out_lines: list[str] = []
    for k, v in args.items():
        text = str(v)
        if len(text) > 200:
            text = text[:200] + " …"
        out_lines.append(f"  {k}: {text}")
    return "\n".join(out_lines) or "  (no args)"


def _run_one(agent: Agent, prompt: str, no_stream: bool) -> int:
    t0 = time.time()
    if no_stream:
        agent.d.on_chunk = None
    answer = agent.turn(prompt)
    elapsed = time.time() - t0
    if not no_stream and agent.d.on_chunk is not None:
        console.print()
    elif answer.strip():
        console.print(Markdown(answer))
    stats = _stats_line(agent)
    console.print(f"[dim]{stats} {elapsed:.1f}s {_session_line(agent)}[/dim]")
    return 0


def _finish(agent: Agent, deps, cfg: Config) -> None:
    try:
        agent.end_session()
    except Exception:
        pass
    deps.store.end_session(agent.session_id)
    if cfg.auto_dream_on_exit:
        try:
            res = consolidate(cfg)
            if not res.get("skipped"):
                console.print(f"[dim]auto-dream: {res['summary']}[/dim]")
        except Exception as e:
            console.print(f"[yellow]auto-dream failed: {e}[/yellow]")
    deps.store.close()
    deps.brain.client.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = _cfg_from_args(args)

    if args.tune:
        return cmd_tune()
    if args.doctor:
        return cmd_doctor(cfg, json_out=args.json)

    session_state: dict = {"yolo": False}
    cancel_event = threading.Event()

    def confirm_shell(cmd: str) -> bool:
        if session_state.get("yolo"):
            return True
        console.print(Panel(
            f"[bold]Command requested outside the default shell policy[/bold]\n\n"
            f"[cyan]{cmd}[/cyan]\n\n"
            f"[dim]workspace: {cfg.workspace}[/dim]",
            title="shell approval", border_style="yellow",
        ))
        return Confirm.ask("Run it anyway?", default=False)

    on_chunk = None if args.no_stream else (lambda ch: console.out(ch, end=""))
    agent, deps = build_agent(
        cfg,
        on_tool_call=render_tool_call,
        on_chunk=on_chunk,
        confirm_callback=confirm_shell,
        cancel=cancel_event,
    )
    _wrap_confirm_gate(agent, cfg, session_state)
    _set_tier(agent, args.tier)

    if not args.no_warm:
        deps.brain.warm(agent._pick_tier())
        warm_note = "model warmed"
    else:
        warm_note = "warm skipped"

    banner(cfg, sandbox_active=deps.shell.sandbox_active())
    console.print(f"[dim]session {agent.session_id} - {warm_note}[/dim]\n")

    if args.resume is not None:
        cmd_resume(agent, args.resume or None)

    prompt = args.prompt_opt or (" ".join(args.prompt).strip() if args.prompt else "")
    if prompt:
        try:
            return _run_one(agent, prompt, no_stream=args.no_stream)
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
                if not handle_slash(agent, line, session_state):
                    break
                continue
            cancel_event.clear()
            t0 = time.time()
            try:
                answer = agent.turn(line)
            except KeyboardInterrupt:
                cancel_event.set()
                console.print("\n[yellow]…interrupted[/yellow]")
                continue
            elapsed = time.time() - t0
            if not args.no_stream and on_chunk is not None:
                console.print()
            elif answer.strip():
                console.print(Markdown(answer))
            stats = _stats_line(agent) or f"[{agent._pick_tier()}]"
            console.print(f"[dim]{stats} {elapsed:.1f}s {_session_line(agent)}[/dim]\n")
    finally:
        _finish(agent, deps, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
