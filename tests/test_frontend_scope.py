"""Static guard against cross-IIFE reference errors in web/static/app.js.

app.js is organised as several top-level IIFEs (mapping, transformation,
validation, reconciliation). Each has its own scope, so a helper defined in one
is invisible in another — and because the failure only surfaces when a user
reaches that tab and clicks, it ships silently. That is exactly how
`ReferenceError: valCsv is not defined` reached the reconciliation workspace:
`valCsv`, `valFeedCard` and `showTextModal` all live in the validation module.

Shared helpers must be exported onto `window`; module-private state (VAL, REC)
must not be reached across modules at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path("web/static/app.js")

# helpers and private state that have, or could, cross a module boundary
WATCHED = ("VAL", "REC", "valCsv", "valFeedCard", "showTextModal",
           "evidenceHtml", "sampleTable", "sampleCsv", "wireEvidenceDownloads",
           "markRail", "checkCard", "renderRecInputs", "EV_SAMPLES",
           "renderValInputs", "renderValResults", "groupedChecks")
# state objects that are deliberately private to one module
PRIVATE = {"VAL", "REC", "EV_SAMPLES"}


def _strip_comments(src: str) -> str:
    """Remove // line comments and /* */ blocks.

    Without this the export scan matches `// window.valCsv = valCsv;` and the
    guard passes on exactly the code it exists to reject — which is what
    happened the first time this test was written.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.splitlines():
        idx, in_str, quote, esc = None, False, "", False
        for i, ch in enumerate(line):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if in_str:
                if ch == quote:
                    in_str = False
            elif ch in "\"'`":
                in_str, quote = True, ch
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                idx = i
                break
        out.append(line if idx is None else line[:idx])
    return "\n".join(out)


def _iife_spans(lines: list[str]) -> list[tuple[int, int]]:
    spans, start = [], None
    for i, line in enumerate(lines, 1):
        if re.match(r"^\(function\s*\(\)", line):
            start = i
        elif re.match(r"^\}\)\(\);", line) and start:
            spans.append((start, i))
            start = None
    return spans


@pytest.mark.skipif(not APP.exists(), reason="frontend not present")
def test_no_identifier_is_used_outside_the_scope_that_defines_it():
    src = _strip_comments(APP.read_text(encoding="utf-8"))
    lines = src.splitlines()
    spans = _iife_spans(lines)
    assert len(spans) >= 3, "expected several top-level IIFEs in app.js"

    file_globals = set(re.findall(r"^(?:function|const|let|var)\s+(\w+)", src, re.M))
    exported = set(re.findall(r"window\.(\w+)\s*=", src))

    problems = []
    for a, b in spans:
        body = "\n".join(lines[a - 1:b])
        local = set(re.findall(r"\b(?:function|const|let|var)\s+(\w+)", body))
        for name in WATCHED:
            if not re.search(rf"(?<!\.)\b{name}\b", body):
                continue
            if name in local or name in file_globals or name in exported:
                continue
            problems.append(f"IIFE {a}-{b}: '{name}' is neither local, global, "
                            f"nor exported on window")
    assert not problems, "\n".join(problems)


@pytest.mark.skipif(not APP.exists(), reason="frontend not present")
def test_module_private_state_is_not_exported():
    """VAL and REC hold per-workspace state. Exporting them would invite the
    modules to read each other's internals instead of going through a helper
    like valCsv(), which is where the 'which CSV is under test' rule lives."""
    src = _strip_comments(APP.read_text(encoding="utf-8"))
    for name in PRIVATE:
        assert not re.search(rf"window\.{name}\s*=", src), \
            f"{name} is module-private and must not be exported"


def test_index_stamps_asset_urls_with_a_content_hash():
    """Hand-edited `?v=` strings are a silent failure mode: edit app.js, forget
    to bump the version, and every returning browser keeps running the cached
    previous build — which is indistinguishable from the fix not working. The
    version must be derived from file content so it cannot be forgotten."""
    import re as _re
    from fastapi.testclient import TestClient
    from api import server

    client = TestClient(server.app)
    html = client.get("/").text
    assert client.get("/").headers.get("cache-control") == "no-store"

    versions = dict(_re.findall(r'(?:href|src)="/static/([\w./-]+)\?v=(\w+)', html))
    assert "app.js" in versions and "styles.css" in versions
    assert all(len(v) >= 8 for v in versions.values()), versions

    app_js = server.STATIC / "app.js"
    original = app_js.read_bytes()
    try:
        before = versions["app.js"]
        app_js.write_bytes(original + b"\n// touch\n")
        after = dict(_re.findall(r'src="/static/([\w./-]+)\?v=(\w+)',
                                 client.get("/").text))["app.js"]
        assert after != before, "editing app.js must change its cache-busting version"
    finally:
        app_js.write_bytes(original)
