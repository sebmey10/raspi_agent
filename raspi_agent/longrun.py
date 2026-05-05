"""Controlled long-running research and project iteration loops.

The loop is intentionally not a runaway daemon. A run persists its topic,
cycle summaries, event log, and child-agent lanes in SQLite. `--forever` keeps
cycling until interrupted, but each cycle is one normal bounded Agent turn with
the same tool limits, reflection, context compaction, and sandbox rules as the
interactive agent.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from .agent import Agent, build_agent
from .config import CONFIG, Config
from .llm import TIER_FAST, TIER_REASONER
from .memory.consolidate import consolidate
from .memory.store import Store
from .tools.registry import Tool

ACTIVE_LANE_STATUSES = {"active", "waiting"}
TERMINAL_RUN_STATUSES = {"complete", "cancelled", "error"}


@dataclass(frozen=True)
class LongRunOptions:
    cycles: int = 3
    interval_s: float = 0.0
    max_child_lanes: int = 3
    forever: bool = False
    compact_every: int = 4
    tier: str = "auto"


CycleCallback = Callable[[str, dict[str, Any]], None]
ToolCallback = Callable[[str, dict, str], None]


def run_research(
    cfg: Config = CONFIG,
    *,
    topic: str | None = None,
    run_id: str | None = None,
    options: LongRunOptions | None = None,
    on_cycle: CycleCallback | None = None,
    on_tool_call: ToolCallback | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Create or continue a durable research run."""
    options = options or LongRunOptions()
    if not topic and not run_id:
        raise ValueError("topic or run_id is required")
    cancel = cancel or threading.Event()

    agent, deps = build_agent(
        cfg,
        on_tool_call=on_tool_call,
        on_chunk=None,
        cancel=cancel,
        reset_scratch=False,
        preserve_context=True,
    )
    _set_tier(agent, options.tier)
    store = deps.store
    last_answer = ""
    cycles_done = 0

    try:
        if run_id:
            run = store.get_long_run(run_id)
            if not run:
                raise ValueError(f"unknown long run: {run_id}")
            state = _ensure_state(run["topic"], run.get("state"), run["max_child_lanes"])
            store.update_long_run(
                run_id,
                status="active" if run["status"] not in TERMINAL_RUN_STATUSES else run["status"],
                state=state,
                session_id=agent.session_id,
            )
        else:
            assert topic is not None
            max_cycles = None if options.forever else max(1, options.cycles)
            state = _ensure_state(topic, {}, options.max_child_lanes)
            run_id = store.create_long_run(
                topic,
                max_cycles=max_cycles,
                interval_s=options.interval_s,
                max_child_lanes=options.max_child_lanes,
                state=state,
                session_id=agent.session_id,
            )

        assert run_id is not None
        agent.install_tool(_child_agent_tool(store, run_id, options.max_child_lanes))
        if options.forever:
            store.update_long_run(run_id, max_cycles=None, status="active")

        invocation_limit = None if options.forever else max(0, options.cycles)
        while not cancel.is_set():
            run = store.get_long_run(run_id)
            if not run:
                raise ValueError(f"long run disappeared: {run_id}")
            if run["status"] in TERMINAL_RUN_STATUSES:
                break
            if _hit_stored_cycle_cap(run):
                store.update_long_run(run_id, status="complete")
                _event(store, run_id, "complete", "stored cycle cap reached")
                break
            if invocation_limit is not None and cycles_done >= invocation_limit:
                store.update_long_run(run_id, status="paused")
                break

            state = _ensure_state(run["topic"], run.get("state"), run["max_child_lanes"])
            lane = _select_lane(state)
            cycle_no = int(run["cycles"]) + 1
            store.update_long_run(run_id, status="active", state=state)
            _notify(on_cycle, "start", run=run, cycle=cycle_no, lane=lane)

            prompt = _cycle_prompt(run, state, lane, cycle_no)
            try:
                last_answer = agent.turn(prompt)
            except Exception as e:
                store.update_long_run(run_id, status="error")
                _event(store, run_id, "error", f"{type(e).__name__}: {e}")
                raise

            run_after_tools = store.get_long_run(run_id) or run
            state = _ensure_state(
                run_after_tools["topic"],
                run_after_tools.get("state"),
                run_after_tools["max_child_lanes"],
            )
            state = _record_cycle_summary(state, lane, cycle_no, last_answer)
            cycles_total = int(run_after_tools["cycles"]) + 1
            status = "complete" if _will_hit_cap(run_after_tools, cycles_total) else "active"
            store.update_long_run(
                run_id,
                cycles=cycles_total,
                status=status,
                state=state,
                interval_s=options.interval_s,
                max_child_lanes=options.max_child_lanes,
            )
            _event(
                store,
                run_id,
                "cycle",
                _clip(last_answer, 4000),
                {
                    "cycle": cycle_no,
                    "lane": lane.get("id"),
                    "lane_title": lane.get("title"),
                    "status": status,
                },
            )
            cycles_done += 1
            _notify(
                on_cycle,
                "done",
                run=store.get_long_run(run_id) or run,
                cycle=cycle_no,
                lane=lane,
                answer=last_answer,
            )

            if status == "complete":
                break
            if options.compact_every > 0 and cycle_no % options.compact_every == 0:
                _run_consolidation(cfg, store, run_id)
            if options.interval_s > 0 and not cancel.is_set():
                _notify(on_cycle, "sleep", run=run, seconds=options.interval_s)
                if cancel.wait(options.interval_s):
                    break

        final_run = store.get_long_run(run_id) or {"id": run_id, "status": "unknown"}
        if cancel.is_set() and final_run.get("status") == "active":
            store.update_long_run(run_id, status="paused")
            final_run = store.get_long_run(run_id) or final_run
        _notify(on_cycle, "stop", run=final_run, cycles_done=cycles_done)
        final_run["cycles_done"] = cycles_done
        final_run["last_answer"] = last_answer
        return final_run
    finally:
        _close_agent(agent, deps)


def _set_tier(agent: Agent, tier: str) -> None:
    if tier == "fast":
        agent.forced_tier = TIER_FAST
    elif tier == "reasoner":
        agent.forced_tier = TIER_REASONER
    else:
        agent.forced_tier = None


def _close_agent(agent: Agent, deps) -> None:
    try:
        agent.end_session()
    except Exception:
        pass
    try:
        deps.store.end_session(agent.session_id)
    except Exception:
        pass
    try:
        deps.store.close()
    finally:
        deps.brain.client.close()


def _notify(callback: CycleCallback | None, event: str, **payload: Any) -> None:
    if callback is None:
        return
    try:
        callback(event, payload)
    except Exception:
        pass


def _event(
    store: Store,
    run_id: str,
    kind: str,
    content: str,
    data: dict[str, Any] | None = None,
) -> None:
    store.append_long_run_event(run_id, kind, content, data)


def _hit_stored_cycle_cap(run: dict[str, Any]) -> bool:
    max_cycles = run.get("max_cycles")
    return max_cycles is not None and int(run.get("cycles") or 0) >= int(max_cycles)


def _will_hit_cap(run: dict[str, Any], cycles_total: int) -> bool:
    max_cycles = run.get("max_cycles")
    return max_cycles is not None and cycles_total >= int(max_cycles)


def _ensure_state(
    topic: str,
    state: dict[str, Any] | None,
    max_child_lanes: int,
) -> dict[str, Any]:
    clean: dict[str, Any] = dict(state or {})
    clean.setdefault("version", 1)
    clean.setdefault("topic", topic)
    clean.setdefault("lane_cursor", 0)
    clean.setdefault("next_lane_id", 2)
    clean.setdefault("cycle_summaries", [])
    lanes = clean.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        lanes = [{
            "id": "lane-1",
            "title": "Primary research",
            "goal": topic,
            "status": "active",
            "notes": [],
            "cycles": 0,
        }]
    # Keep the primary lane plus the configured number of child lanes.
    clean["lanes"] = lanes[:max(1, max_child_lanes + 1)]
    return clean


def _select_lane(state: dict[str, Any]) -> dict[str, Any]:
    lanes = state.get("lanes") or []
    active = [lane for lane in lanes if lane.get("status") in ACTIVE_LANE_STATUSES]
    if not active:
        lanes[0]["status"] = "active"
        active = [lanes[0]]
    cursor = int(state.get("lane_cursor") or 0)
    lane = active[cursor % len(active)]
    state["lane_cursor"] = cursor + 1
    return lane


def _record_cycle_summary(
    state: dict[str, Any],
    lane: dict[str, Any],
    cycle_no: int,
    answer: str,
) -> dict[str, Any]:
    summaries = list(state.get("cycle_summaries") or [])
    summaries.append({
        "cycle": cycle_no,
        "lane": lane.get("id"),
        "summary": _clip(answer.strip().replace("\n", " "), 1200),
    })
    state["cycle_summaries"] = summaries[-12:]
    for existing in state.get("lanes", []):
        if existing.get("id") == lane.get("id"):
            existing["cycles"] = int(existing.get("cycles") or 0) + 1
            notes = list(existing.get("notes") or [])
            notes.append(_clip(answer.strip().replace("\n", " "), 500))
            existing["notes"] = notes[-6:]
            break
    return state


def _cycle_prompt(
    run: dict[str, Any],
    state: dict[str, Any],
    lane: dict[str, Any],
    cycle_no: int,
) -> str:
    max_cycles = run.get("max_cycles")
    cap = "unbounded" if max_cycles is None else str(max_cycles)
    lane_lines = []
    for item in state.get("lanes", []):
        lane_lines.append(
            f"- {item.get('id')}: {item.get('title')} "
            f"[{item.get('status')}] goal={item.get('goal', '')}"
        )
    summary_lines = []
    for item in state.get("cycle_summaries", [])[-6:]:
        summary_lines.append(
            f"- cycle {item.get('cycle')} {item.get('lane')}: {item.get('summary')}"
        )
    lanes = "\n".join(lane_lines) or "- lane-1: Primary research [active]"
    summaries = "\n".join(summary_lines) or "- none yet"
    return f"""[long-run-research-cycle]
Run id: {run["id"]}
Topic: {run["topic"]}
Cycle: {cycle_no} of {cap}
Active child-agent lane: {lane.get("id")} - {lane.get("title")}
Lane goal: {lane.get("goal", run["topic"])}

Child-agent lanes:
{lanes}

Compressed prior cycle summaries:
{summaries}

Do exactly one bounded research/project iteration for the active lane.
Use tools when useful. Prefer primary sources, local repo evidence, and concrete
artifacts over vibes. If the topic needs parallel attention, call
`child_agent` to create or update a bounded child lane. Do not create more
lanes than needed. Use `context_update` for facts that must survive context
compaction, and `remember` only for durable memory.

End with these short sections:
Cycle summary:
Evidence:
Child lanes:
Next step:
"""


def _child_agent_tool(store: Store, run_id: str, max_child_lanes: int) -> Tool:
    return Tool(
        name="child_agent",
        description=(
            "Create or update a persistent bounded child-agent lane for a long-run "
            "research/project run. This records work lanes; it does not spawn an "
            "unlimited OS process."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "complete", "block"],
                },
                "title": {"type": "string"},
                "goal": {"type": "string"},
                "lane_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["action"],
        },
        handler=lambda args: _apply_child_agent_action(
            store, run_id, max_child_lanes, args
        ),
    )


def _apply_child_agent_action(
    store: Store,
    run_id: str,
    max_child_lanes: int,
    args: dict[str, Any],
) -> str:
    run = store.get_long_run(run_id)
    if not run:
        return f"<error>unknown long run: {run_id}</error>"
    state = _ensure_state(run["topic"], run.get("state"), max_child_lanes)
    action = str(args.get("action") or "").strip().lower()
    title = str(args.get("title") or "").strip()
    goal = str(args.get("goal") or "").strip()
    lane_id = str(args.get("lane_id") or "").strip()
    note = str(args.get("note") or "").strip()

    if action == "create":
        if not title:
            return "<error>title is required when creating a child agent</error>"
        active_count = sum(
            1
            for lane in state["lanes"]
            if lane.get("id") != "lane-1" and lane.get("status") in ACTIVE_LANE_STATUSES
        )
        if active_count >= max(0, max_child_lanes):
            return f"<error>child-agent lane cap reached: {max_child_lanes}</error>"
        for lane in state["lanes"]:
            if lane.get("title", "").strip().lower() == title.lower():
                return f"<child_agent id={lane['id']} duplicate=true/>"
        lane_id = f"lane-{int(state.get('next_lane_id') or 1)}"
        state["next_lane_id"] = int(state.get("next_lane_id") or 1) + 1
        lane = {
            "id": lane_id,
            "title": title,
            "goal": goal or title,
            "status": "active",
            "notes": [note] if note else [],
            "cycles": 0,
        }
        state["lanes"].append(lane)
        store.update_long_run(run_id, state=state)
        _event(store, run_id, "lane_created", title, {"lane_id": lane_id, "goal": goal})
        return f"<child_agent id={lane_id} status=active/>"

    lane = _find_lane(state, lane_id, title)
    if lane is None:
        return "<error>lane_id or existing title is required</error>"
    if action == "update":
        lane["status"] = "active"
    elif action == "complete":
        lane["status"] = "done"
    elif action == "block":
        lane["status"] = "blocked"
    else:
        return "<error>action must be one of: create, update, complete, block</error>"
    if goal:
        lane["goal"] = goal
    if note:
        notes = list(lane.get("notes") or [])
        notes.append(note)
        lane["notes"] = notes[-6:]
    store.update_long_run(run_id, state=state)
    _event(
        store,
        run_id,
        f"lane_{action}",
        lane.get("title", ""),
        {"lane_id": lane.get("id"), "note": note},
    )
    return f"<child_agent id={lane.get('id')} status={lane.get('status')}/>"


def _find_lane(
    state: dict[str, Any],
    lane_id: str,
    title: str,
) -> dict[str, Any] | None:
    for lane in state.get("lanes", []):
        if lane_id and lane.get("id") == lane_id:
            return lane
        if title and lane.get("title", "").strip().lower() == title.lower():
            return lane
    return None


def _run_consolidation(cfg: Config, store: Store, run_id: str) -> None:
    try:
        result = consolidate(cfg)
    except Exception as e:
        _event(store, run_id, "compact_error", f"{type(e).__name__}: {e}")
        return
    kind = "compact_skipped" if result.get("skipped") else "compact"
    content = result.get("reason") or result.get("summary") or kind
    _event(store, run_id, kind, str(content), result)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n<truncated chars={len(text)}>"
