from __future__ import annotations

from pathlib import Path

from rasp_agent.llm import _try_extract_tool_calls_from_text
from rasp_agent.tools import fs
from rasp_agent.tools.shell import ShellGate


def test_extracts_nested_tool_json_from_xml_block():
    text = (
        "<tool_call>"
        '{"name":"write","arguments":{"path":"data.json","content":"{\\"ok\\": true}"}}'
        "</tool_call>"
    )

    calls = _try_extract_tool_calls_from_text(text)

    assert calls == [
        {
            "name": "write",
            "arguments": {"path": "data.json", "content": '{"ok": true}'},
        }
    ]


def test_shell_gate_checks_every_command_in_a_pipeline(tmp_path: Path):
    gate = ShellGate(("echo", "wc", "ls"), timeout=5, workspace=tmp_path)

    assert gate.is_allowed("echo hello | wc -c")
    assert not gate.is_allowed("ls; rm -rf .")
    assert not gate.is_allowed("echo hello > out.txt")
    assert not gate.is_allowed("echo $(rm -rf .)")


def test_workspace_search_returns_relative_matches(tmp_path: Path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.txt").write_text("alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "notes" / "b.txt").write_text("nothing\n", encoding="utf-8")

    result = fs.search(tmp_path, "needle", "notes", max_matches=10)

    assert "<search" in result
    assert "notes/a.txt:2:needle here" in result
    assert str(tmp_path) not in result
