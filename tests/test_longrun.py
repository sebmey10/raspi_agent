from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from raspi_agent import longrun
from raspi_agent.config import CONFIG
from raspi_agent.longrun import LongRunOptions, _apply_child_agent_action
from raspi_agent.memory.store import Store


def test_store_persists_long_runs_and_events(tmp_path):
    db = tmp_path / "db.sqlite"
    store = Store(db)
    try:
        run_id = store.create_long_run(
            "Research tiny local agents",
            max_cycles=5,
            interval_s=1.5,
            max_child_lanes=2,
            state={"lanes": []},
        )
        store.update_long_run(run_id, cycles=1, status="paused", state={"ok": True})
        store.append_long_run_event(run_id, "cycle", "summary", {"cycle": 1})

        run = store.get_long_run(run_id)
        events = store.long_run_events(run_id)

        assert run is not None
        assert run["status"] == "paused"
        assert run["cycles"] == 1
        assert run["state"] == {"ok": True}
        assert [event["kind"] for event in events] == ["created", "cycle"]
        assert events[-1]["data"] == {"cycle": 1}
    finally:
        store.close()


def test_child_agent_lanes_are_capped_and_updatable(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    try:
        run_id = store.create_long_run("topic", max_child_lanes=1)

        first = _apply_child_agent_action(
            store,
            run_id,
            1,
            {"action": "create", "title": "papers", "goal": "survey papers"},
        )
        second = _apply_child_agent_action(
            store,
            run_id,
            1,
            {"action": "create", "title": "repos", "goal": "survey repos"},
        )
        done = _apply_child_agent_action(
            store,
            run_id,
            1,
            {"action": "complete", "lane_id": "lane-2", "note": "enough"},
        )
        run = store.get_long_run(run_id)

        assert first == "<child_agent id=lane-2 status=active/>"
        assert "cap reached" in second
        assert done == "<child_agent id=lane-2 status=done/>"
        assert run is not None
        assert run["state"]["lanes"][1]["status"] == "done"
    finally:
        store.close()


class _FakeAgent:
    def __init__(self, store: Store):
        self.session_id = store.start_session("fake")
        self.forced_tier = None
        self.prompts: list[str] = []
        self.child_tool = None

    def install_tool(self, tool):
        self.child_tool = tool

    def turn(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            assert self.child_tool is not None
            self.child_tool.handler({
                "action": "create",
                "title": "model survey",
                "goal": "Find strong local research models",
            })
        return f"Cycle summary: completed cycle {len(self.prompts)}\nEvidence: fake"

    def end_session(self) -> None:
        pass


def test_run_research_uses_bounded_cycles_and_records_lanes(tmp_path, monkeypatch):
    cfg = replace(
        CONFIG,
        data_dir=tmp_path / "data",
        workspace=tmp_path / "ws",
        sandbox="off",
    )
    cfg.ensure_dirs()

    def fake_build_agent(*args, **kwargs):
        store = Store(cfg.db_path)
        agent = _FakeAgent(store)
        deps = SimpleNamespace(
            store=store,
            brain=SimpleNamespace(client=SimpleNamespace(close=lambda: None)),
        )
        return agent, deps

    monkeypatch.setattr(longrun, "build_agent", fake_build_agent)

    result = longrun.run_research(
        cfg,
        topic="all-day local model research",
        options=LongRunOptions(cycles=2, compact_every=0, max_child_lanes=2),
    )

    store = Store(cfg.db_path)
    try:
        run = store.get_long_run(result["id"])
        events = store.long_run_events(result["id"])
    finally:
        store.close()

    assert run is not None
    assert run["status"] == "complete"
    assert run["cycles"] == 2
    assert any(lane["title"] == "model survey" for lane in run["state"]["lanes"])
    assert [event["kind"] for event in events].count("cycle") == 2
