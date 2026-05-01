from __future__ import annotations

import sys
import time

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .agent import Agent, build_agent
from .config import CONFIG
from .llm import TIER_FAST, TIER_REASONER
from .memory.consolidate import consolidate

console = Console()


def banner() -> None:
    cfg = CONFIG
    console.print(Panel.fit(
        f"[bold cyan]rasp[/bold cyan] — on-board agent\n"
        f"fast: [green]{cfg.model_fast}[/green]   reasoner: [yellow]{cfg.model_reasoner}[/yellow]\n"
        f"workspace: [magenta]{cfg.workspace}[/magenta]\n"
        f"data:      [dim]{cfg.data_dir}[/dim]\n"
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


def main() -> int:
    banner()
    agent, deps = build_agent(on_tool_call=render_tool_call)
    deps.brain.warm(TIER_FAST)
    console.print(f"[dim]session {agent.session_id} started — model warmed[/dim]\n")

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
            tier_tag = f"[{agent._pick_tier()}]"
            if answer.strip():
                console.print(Markdown(answer))
            console.print(f"[dim]{tier_tag} {elapsed:.1f}s[/dim]\n")
    finally:
        deps.store.end_session(agent.session_id)
        deps.brain.client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
