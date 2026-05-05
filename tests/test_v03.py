"""v0.3 additions: scratchpad plan file, apply_patch, dedupe normalization."""
from __future__ import annotations

from pathlib import Path

from raspi_agent.agent import Agent
from raspi_agent.memory.scratchpad import Scratchpad
from raspi_agent.tools import edit_ladder


# ---- Scratchpad -------------------------------------------------------------


def test_scratchpad_pins_user_task_above_plan(tmp_path: Path):
    sp = Scratchpad.from_workspace(tmp_path)
    sp.start_session()
    sp.begin_task("rename foo to bar across the repo")
    sp.write_plan(["read fs.py", "ast_edit foo -> bar", "run tests"])
    text = sp.plan_path.read_text(encoding="utf-8")

    assert text.startswith("_user-task: rename foo to bar across the repo_")
    assert "## Plan" in text
    assert "1. read fs.py" in text
    assert "3. run tests" in text


def test_scratchpad_replan_preserves_pin(tmp_path: Path):
    sp = Scratchpad.from_workspace(tmp_path)
    sp.start_session()
    sp.begin_task("first task")
    sp.write_plan(["a"])
    pin_text = sp._read_pin()  # noqa: SLF001 - test private API
    sp.write_plan(["b", "c"])
    second = sp.plan_path.read_text(encoding="utf-8")
    assert second.startswith(pin_text)
    assert "1. b" in second
    assert "2. c" in second
    assert "1. a" not in second


def test_scratchpad_updates_plan_step_status(tmp_path: Path):
    sp = Scratchpad.from_workspace(tmp_path)
    sp.start_session()
    sp.begin_task("ship it")
    sp.write_plan(["read", "edit"])

    assert sp.update_plan_step(1, "doing", "opened file") == "<plan_step step=1 status=doing/>"
    assert sp.update_plan_step(1, "done") == "<plan_step step=1 status=done/>"
    text = sp.plan_path.read_text(encoding="utf-8")

    assert "- [x] 1. read" in text
    assert "- [ ] 2. edit" in text


def test_scratchpad_session_start_clears_old_state(tmp_path: Path):
    sp = Scratchpad.from_workspace(tmp_path)
    sp.start_session()
    sp.begin_task("old task")
    sp.write_plan(["leftover"])
    sp.update_context("Decisions", "Keep this only for old session")
    assert sp.plan_path.exists()
    assert sp.context_path.exists()

    sp2 = Scratchpad.from_workspace(tmp_path)
    sp2.start_session()
    assert not sp2.plan_path.exists()
    assert not sp2.context_path.exists()
    assert (tmp_path / ".raspi" / ".gitignore").exists()


def test_scratchpad_context_update_dedupes_and_renders(tmp_path: Path):
    sp = Scratchpad.from_workspace(tmp_path)
    sp.start_session()

    assert "<context" in sp.update_context("files", "Read README.md")
    sp.update_context("Files Inspected", "Read README.md")
    text = sp.render_context_for_prompt(1000)

    assert "## Files Inspected" in text
    assert text.count("Read README.md") == 1


# ---- apply_patch ------------------------------------------------------------


def test_apply_patch_applies_a_clean_hunk(tmp_path: Path):
    p = tmp_path / "hello.py"
    p.write_text("def hello():\n    print('hi')\n", encoding="utf-8")

    patch = (
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    print('hi')\n"
        "+    print('hello, world')\n"
    )
    res = edit_ladder.apply_patch(tmp_path, "hello.py", patch)
    assert "<patched" in res
    assert p.read_text(encoding="utf-8") == "def hello():\n    print('hello, world')\n"


def test_apply_patch_rejects_context_mismatch(tmp_path: Path):
    p = tmp_path / "hello.py"
    p.write_text("def hello():\n    print('hi')\n", encoding="utf-8")
    bad = (
        "@@ -1,2 +1,2 @@\n"
        " def goodbye():\n"
        "-    print('hi')\n"
        "+    print('bye')\n"
    )
    res = edit_ladder.apply_patch(tmp_path, "hello.py", bad)
    assert "<error>" in res
    # File untouched.
    assert "hi" in p.read_text(encoding="utf-8")


# ---- dedupe normalization ---------------------------------------------------


def test_normalize_args_treats_whitespace_and_key_order_identically():
    a = Agent._normalize_args({"path": "  README.md  ", "max_lines": 100})
    b = Agent._normalize_args({"max_lines": 100, "path": "README.md"})
    assert a == b
