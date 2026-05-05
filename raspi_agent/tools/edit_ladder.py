"""Edit ladder tools: ast_edit (tree-sitter) and apply_patch (unified diff).

The agent's preferred edit order is:
  1. ast_edit  (semantic, syntax-safe; Python/JS only)
  2. apply_patch  (unified diff; multi-hunk, format-preserving)
  3. edit  (single-replace; in tools/fs.py)
  4. write  (full overwrite; last resort)

ast_edit is import-guarded: if tree-sitter wheels are absent, the tool
returns an error and doctor reports it disabled.
"""
from __future__ import annotations

import re
import time
from html import escape
from pathlib import Path

from . import fs

try:
    import tree_sitter as _ts
    import tree_sitter_python as _ts_py
    import tree_sitter_javascript as _ts_js
    AST_AVAILABLE = True
except Exception:  # pragma: no cover — runtime feature gate
    _ts = None
    _ts_py = None
    _ts_js = None
    AST_AVAILABLE = False


_TS_LANG_CACHE: dict[str, object] = {}


def _ts_language(name: str):
    if not AST_AVAILABLE:
        return None
    if name in _TS_LANG_CACHE:
        return _TS_LANG_CACHE[name]
    if name in {"python", "py"}:
        lang = _ts.Language(_ts_py.language())
    elif name in {"javascript", "js"}:
        lang = _ts.Language(_ts_js.language())
    else:
        return None
    _TS_LANG_CACHE[name] = lang
    return lang


# ---- ast_edit -----------------------------------------------------------------


_PY_KIND_NODES = {
    "function": ("function_definition",),
    "method": ("function_definition",),
    "class": ("class_definition",),
}
_JS_KIND_NODES = {
    "function": ("function_declaration", "function", "arrow_function", "method_definition"),
    "method": ("method_definition",),
    "class": ("class_declaration",),
}


def ast_edit(
    workspace: Path,
    path: str,
    language: str,
    kind: str,
    name: str,
    replacement: str,
) -> str:
    """Replace a Python or JS named definition (function/class/method) with new text.

    The match is by `kind` + `name`. If multiple definitions share the name,
    the call fails — ask the model to pick a unique target via apply_patch.
    """
    if not AST_AVAILABLE:
        return ("<error>ast_edit unavailable: tree-sitter wheels not installed "
                "(install raspi-agent[ast] to enable)</error>")
    lang = _ts_language(language.lower())
    if lang is None:
        return f"<error>unsupported language: {language!r} (supported: python, javascript)</error>"
    kind_l = kind.lower()
    kinds = _PY_KIND_NODES if language.lower() in {"python", "py"} else _JS_KIND_NODES
    if kind_l not in kinds:
        return f"<error>unsupported kind: {kind!r} (use {', '.join(sorted(kinds))})</error>"
    target_types = kinds[kind_l]

    p = fs._resolve_in(workspace, path)
    if not p.exists() or not p.is_file():
        return f"<error>file not found: {path}</error>"
    source = p.read_bytes()

    parser = _ts.Parser(lang)
    tree = parser.parse(source)

    matches: list[tuple[int, int]] = []
    cursor = tree.walk()
    _walk_collect(cursor, target_types, name, matches)
    if not matches:
        return f"<error>no {kind} named {name!r} found in {path}</error>"
    if len(matches) > 1:
        spans = ", ".join(f"bytes {s}-{e}" for s, e in matches)
        return f"<error>{len(matches)} {kind}s named {name!r} found ({spans}); use apply_patch instead</error>"

    start, end = matches[0]
    repl_bytes = replacement.encode("utf-8")
    new_source = source[:start] + repl_bytes + source[end:]

    # Verify the replacement still parses.
    new_tree = parser.parse(new_source)
    if new_tree.root_node.has_error:
        return ("<error>replacement breaks parse — fix syntax in `replacement` "
                "or fall back to apply_patch</error>")

    _atomic_write(p, new_source)
    return (f"<ast_edited path={escape(path, quote=True)} kind={kind} "
            f"name={escape(name, quote=True)} bytes={end - start} -> {len(repl_bytes)}/>")


def _walk_collect(cursor, target_types, name: str, out: list[tuple[int, int]]) -> None:
    visited_children = False
    while True:
        node = cursor.node
        if not visited_children and node.type in target_types:
            ident = _node_name(node)
            if ident == name:
                out.append((node.start_byte, node.end_byte))
        if not visited_children and cursor.goto_first_child():
            visited_children = False
            continue
        if cursor.goto_next_sibling():
            visited_children = False
            continue
        if not cursor.goto_parent():
            return
        visited_children = True


def _node_name(node) -> str | None:
    """Best-effort identifier extraction from a definition node."""
    # Tree-sitter exposes a `name` field on function/class/method definitions
    # in both Python and JS grammars.
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    try:
        return name_node.text.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None


# ---- apply_patch (unified diff) ----------------------------------------------


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def apply_patch(workspace: Path, path: str, patch: str) -> str:
    """Apply a unified-diff patch to a single file.

    Accepts patch text containing one or more hunks (`@@ ... @@`). Each hunk's
    context lines must match the file exactly; otherwise the call fails before
    any write. Multi-file patches are not supported — call once per file.
    """
    p = fs._resolve_in(workspace, path)
    if not p.exists() or not p.is_file():
        return f"<error>file not found: {path}</error>"

    hunks = _parse_unified_hunks(patch)
    if not hunks:
        return "<error>patch contains no hunks (`@@ ... @@`)</error>"

    original = p.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    new_lines, applied = _apply_hunks(lines, hunks)
    if applied is None:
        return new_lines  # error string in this branch
    new_text = "".join(new_lines)
    if new_text == original:
        return "<error>patch produced no changes — already applied?</error>"
    _atomic_write(p, new_text.encode("utf-8"))
    return (f"<patched path={escape(path, quote=True)} hunks={applied} "
            f"bytes={len(original)} -> {len(new_text)}/>")


def _parse_unified_hunks(patch: str) -> list[dict]:
    """Parse a unified-diff body into hunks. Ignores file-header lines."""
    hunks: list[dict] = []
    headers = list(_HUNK_RE.finditer(patch))
    if not headers:
        return []
    for i, m in enumerate(headers):
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) else 1
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) else 1
        body_start = m.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(patch)
        body = patch[body_start:body_end]
        # Drop leading newline after the header line.
        if body.startswith("\n"):
            body = body[1:]
        hunks.append({
            "old_start": old_start,
            "old_count": old_count,
            "new_start": new_start,
            "new_count": new_count,
            "body": body,
        })
    return hunks


def _apply_hunks(file_lines: list[str], hunks: list[dict]):
    """Apply hunks in-order. Returns (new_lines, count) on success or
    (error_string, None) on failure."""
    out: list[str] = []
    cursor = 0  # index into file_lines we have copied through
    applied = 0
    for h in hunks:
        target = h["old_start"] - 1
        # Copy unchanged lines up to where this hunk begins.
        if target < cursor:
            return f"<error>overlapping or out-of-order hunks (target {h['old_start']}, cursor {cursor + 1})</error>", None
        out.extend(file_lines[cursor:target])
        cursor = target

        # Walk the hunk body: ' ' = context, '-' = delete, '+' = add.
        body_lines = h["body"].splitlines(keepends=True)
        for raw in body_lines:
            if not raw or raw == "\n":
                # Blank delimiter inside hunk body — treat as context-empty.
                continue
            tag, text = raw[0], raw[1:]
            if tag == " " or tag == "\\":
                # Context (or "\ No newline at end of file" marker).
                if tag == "\\":
                    continue
                if cursor >= len(file_lines):
                    return f"<error>hunk context past EOF at file line {cursor + 1}</error>", None
                if file_lines[cursor].rstrip("\n") != text.rstrip("\n"):
                    return (f"<error>context mismatch at line {cursor + 1}: "
                            f"expected {text.rstrip()!r}, got {file_lines[cursor].rstrip()!r}</error>"), None
                out.append(file_lines[cursor])
                cursor += 1
            elif tag == "-":
                if cursor >= len(file_lines):
                    return f"<error>delete past EOF at line {cursor + 1}</error>", None
                if file_lines[cursor].rstrip("\n") != text.rstrip("\n"):
                    return (f"<error>delete mismatch at line {cursor + 1}: "
                            f"expected {text.rstrip()!r}, got {file_lines[cursor].rstrip()!r}</error>"), None
                cursor += 1
            elif tag == "+":
                # Preserve the trailing newline if present in the patch text;
                # if the original last line lacked one, the patch should follow.
                if not text.endswith("\n"):
                    text = text + "\n"
                out.append(text)
            else:
                return f"<error>unrecognized hunk line {raw!r}</error>", None
        applied += 1

    out.extend(file_lines[cursor:])
    return out, applied


# ---- shared utilities --------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
