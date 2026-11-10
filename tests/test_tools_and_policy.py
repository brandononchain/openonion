from __future__ import annotations

import pytest

from openonion.policy import PolicyEngine, SessionPolicy, check_url, host_matches
from openonion.tools.base import ToolContext, ToolError
from openonion.tools.fs import ReadFile, WriteFile, safe_path
from openonion.tools.web import html_to_text


def test_safe_path_blocks_traversal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert safe_path(ws, "a/b.txt") == (ws / "a" / "b.txt").resolve()
    assert safe_path(ws, "/a.txt") == (ws / "a.txt").resolve()
    for bad in ["../x", "a/../../x", "../../etc/passwd"]:
        with pytest.raises(ToolError):
            safe_path(ws, bad)


def test_safe_path_blocks_symlink_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret").write_text("s")
    (ws / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(ToolError):
        safe_path(ws, "link")


async def test_write_then_read(tmp_path, settings):
    ctx = ToolContext("s", tmp_path, settings, SessionPolicy(), None, None)
    await WriteFile().run(WriteFile.Args(path="d/f.txt", content="hi"), ctx)
    res = await ReadFile().run(ReadFile.Args(path="d/f.txt"), ctx)
    assert res.ok and res.content == "hi"


@pytest.mark.parametrize("url,via_tor,expected", [
    ("https://example.com", False, "allow"),
    ("ftp://example.com", False, "deny"),
    ("file:///etc/passwd", False, "deny"),
    ("http://localhost:8000", False, "deny"),
    ("http://10.0.0.5/", True, "deny"),
    ("http://[::1]/", False, "deny"),
    ("http://metadata.google.internal/", False, "deny"),
    ("http://2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion", False, "deny"),
    ("http://2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion", True, "allow"),
])
def test_check_url(url, via_tor, expected):
    assert check_url(url, via_tor=via_tor, policy=SessionPolicy()).action == expected


def test_allowlists():
    assert host_matches("docs.python.org", ["python.org"])
    assert host_matches("python.org", ["*.python.org"])
    assert not host_matches("evilpython.org", ["python.org"])
    p = SessionPolicy(clearnet_allowlist=["python.org"])
    assert check_url("https://docs.python.org/3/", via_tor=False, policy=p).action == "allow"
    assert check_url("https://example.com", via_tor=False, policy=p).action == "deny"


def test_policy_engine_modes():
    tor_only = PolicyEngine(SessionPolicy(network="tor"))
    assert tor_only.check("web_fetch", {"url": "https://example.com"}).action == "deny"
    assert tor_only.check("tor_browse", {"url": "https://example.com"}).action == "allow"
    offline = PolicyEngine(SessionPolicy(network="off"))
    assert offline.check("tor_new_identity", {}).action == "deny"
    assert offline.check("read_file", {"path": "x"}).action == "allow"
    gated = PolicyEngine(SessionPolicy(approval_required=["write_file"]))
    assert gated.check("write_file", {}).action == "ask"
    limited = PolicyEngine(SessionPolicy(allowed_tools=["read_file"]))
    assert limited.check("write_file", {}).action == "deny"
    assert limited.check("update_plan", {}).action == "allow"


def test_html_to_text_strips_scripts():
    title, text = html_to_text("<html><head><title>T</title><script>evil()</script></head>"
                               "<body><h1>Hi</h1><p>there &amp; back</p><style>x{}</style></body></html>")
    assert title == "T"
    assert "evil" not in text and "x{}" not in text
    assert "Hi" in text and "there & back" in text
