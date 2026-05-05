from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from raspi_agent.agent import Agent, AgentDeps
from raspi_agent.config import CONFIG
from raspi_agent.doctor import _decode_throttled
from raspi_agent.llm import LLMResponse, TIER_FAST, TIER_REASONER, _repair_json, _try_extract_tool_calls_from_text
from raspi_agent.memory.scratchpad import Scratchpad
from raspi_agent.memory.store import Store
from raspi_agent.memory.wiki import Wiki
from raspi_agent.tools import fs
from raspi_agent.tools.registry import Tool, build_tools
from raspi_agent.tools.shell import ShellGate
from raspi_agent.tools.web import _validate_url


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


def test_embedded_json_example_is_not_executed_as_tool_call():
    text = (
        "Here is an example:\n"
        "```json\n"
        '{"name":"bash","arguments":{"cmd":"date"}}\n'
        "```"
    )

    assert _try_extract_tool_calls_from_text(text) == []


def test_plain_json_name_field_is_not_enough_for_tool_call():
    assert _try_extract_tool_calls_from_text('{"name":"Alice"}') == []


def test_extracts_whole_response_fenced_tool_call():
    text = '```json\n{"name":"bash","arguments":{"cmd":"date"}}\n```'

    assert _try_extract_tool_calls_from_text(text) == [
        {"name": "bash", "arguments": {"cmd": "date"}}
    ]


def test_extracts_native_style_tool_calls_from_text():
    text = (
        '{"tool_calls":[{"function":{"name":"read",'
        '"arguments":"{\\"path\\":\\"README.md\\"}"}}]}'
    )

    assert _try_extract_tool_calls_from_text(text) == [
        {"name": "read", "arguments": {"path": "README.md"}}
    ]


def test_shell_gate_checks_every_command_in_a_pipeline(tmp_path: Path):
    gate = ShellGate(("echo", "wc", "ls"), timeout=5, workspace=tmp_path)

    assert gate.is_allowed("echo hello | wc -c")
    assert not gate.is_allowed("ls; rm -rf .")
    assert not gate.is_allowed("echo hello > out.txt")
    assert not gate.is_allowed("echo $(rm -rf .)")


def test_shell_gate_denies_workspace_escape_and_inline_python(tmp_path: Path):
    gate = ShellGate(("cat", "python3", "echo", "curl"), timeout=5, workspace=tmp_path)

    assert not gate.is_allowed("cat /etc/passwd")
    assert not gate.is_allowed("cat ../secret.txt")
    assert not gate.is_allowed("python3 -c 'print(1)'")
    assert not gate.is_allowed("curl file:///etc/passwd")
    assert not gate.is_allowed("curl http://127.0.0.1:11434/api/tags")
    assert "<sh exit=0>" in gate.run("echo hello")


def test_web_fetch_denies_private_targets_by_default():
    assert _validate_url("http://127.0.0.1:11434/api/tags") is not None
    assert _validate_url("http://localhost") is not None
    assert _validate_url("https://example.com") is None


def test_workspace_search_returns_relative_matches(tmp_path: Path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.txt").write_text("alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "notes" / "b.txt").write_text("nothing\n", encoding="utf-8")

    result = fs.search(tmp_path, "needle", "notes", max_matches=10)

    assert "<search" in result
    assert "notes/a.txt:2:needle here" in result
    assert str(tmp_path) not in result


def test_read_supports_start_line_and_list_files_skips_caches(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored.txt").write_text("hidden\n", encoding="utf-8")

    page = fs.read(tmp_path, "a.txt", max_lines=2, start_line=3)
    listed = fs.list_files(tmp_path, ".", "*.txt", max_files=10)

    assert "    3\tthree" in page
    assert "    4\tfour" in page
    assert "    1\tone" not in page
    assert "a.txt" in listed
    assert "ignored.txt" not in listed


def test_wiki_remember_canonicalizes_and_dedupes(tmp_path: Path):
    wiki = Wiki(tmp_path / "WIKI.md")

    first = wiki.append("project", "Make rasp sharper")
    second = wiki.append("Active Projects", "Make rasp sharper")
    bad = wiki.append("random", "Should not create a new section")

    assert "heading='Active Projects'" in first
    assert "duplicate=true" in second
    assert bad.startswith("<error>unknown heading")
    assert wiki.text().count("Make rasp sharper") == 1


class _LoopingBrain:
    def __init__(self):
        self.calls = 0
        self.models = {TIER_FAST: "qwen3:test", TIER_REASONER: "qwen3:test"}

    def supports_tools(self, tier: str) -> bool:
        return True

    def chat(self, messages: list[dict], tools: list[dict] | None, tier: str) -> LLMResponse:
        self.calls += 1
        if tools is None:
            return LLMResponse("final after tools", [], {"message": {}, "model": "qwen3:test"}, tier)
        return LLMResponse(
            "",
            [{"name": "noop", "arguments": {"i": self.calls}}],
            {"message": {}, "model": "qwen3:test"},
            tier,
        )


def test_agent_synthesizes_final_answer_after_max_tool_loops(tmp_path: Path):
    cfg = replace(
        CONFIG,
        data_dir=tmp_path / "data",
        workspace=tmp_path / "ws",
        native_tools="on",
        max_history_messages=12,
        max_tool_loops=3,
        reflect_every=0,
        sandbox="off",
    )
    cfg.ensure_dirs()
    brain = _LoopingBrain()
    store = Store(cfg.db_path)
    wiki = Wiki(cfg.wiki_path)
    scratch = Scratchpad.from_workspace(cfg.workspace)
    scratch.start_session()
    shell = ShellGate((), timeout=5, workspace=cfg.workspace, sandbox_mode="off")
    tools = build_tools(cfg.workspace, shell)
    tools["noop"] = Tool(
        name="noop",
        description="test tool",
        parameters={
            "type": "object",
            "properties": {"i": {"type": "integer"}},
            "required": ["i"],
        },
        handler=lambda args: f"<ok>{args['i']}</ok>",
    )
    agent = Agent(
        AgentDeps(cfg=cfg, brain=brain, store=store, wiki=wiki, scratch=scratch,
                  tools=tools, shell=shell),
        store.start_session(),
    )

    try:
        assert agent.turn("keep going") == "final after tools"
        assert brain.calls == 4
    finally:
        store.close()


def test_json_repair_fixes_trailing_commas_and_unbalanced_braces():
    assert _repair_json('{"a": 1,}') == '{"a": 1}'
    assert _repair_json('{"a": [1, 2,]}') == '{"a": [1, 2]}'
    # missing closing brace
    repaired = _repair_json('{"name":"bash","arguments":{"cmd":"date"}')
    assert repaired is not None
    assert repaired.endswith("}")


def test_repaired_json_is_a_valid_tool_call_payload():
    text = '{"name":"bash","arguments":{"cmd":"date",}'
    calls = _try_extract_tool_calls_from_text(text)
    assert calls == [{"name": "bash", "arguments": {"cmd": "date"}}]


def test_decode_throttled_clean():
    assert _decode_throttled(0) == "clean"


def test_decode_throttled_present_and_history():
    # bit 2 = throttled now, bit 18 = throttled since boot
    decoded = _decode_throttled(0x40004)
    assert "throttled now" in decoded
    assert "throttled since boot" in decoded


def test_decode_throttled_under_voltage_now():
    # bit 0 = under-voltage now, bit 16 = under-voltage since boot
    decoded = _decode_throttled(0x10001)
    assert "under-voltage now" in decoded
    assert "under-voltage since boot" in decoded
