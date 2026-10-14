"""`input.detect_drop` and `clipboard.paste`: the TUI reads `{matched, text}` and `{attached, message}`, the gateway
stubs answered `{images, files}` and `{text, images, files}`. Every dropped file went in as plain text and an image
paste printed "No image found in clipboard" although no image paste exists."""

from __future__ import annotations

import pytest

from k3code.gateway import server as gateway_server
from test_autonomy_gateway import call, make, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}


@pytest.fixture
async def server(tmp_path, monkeypatch):
    srv = make(tmp_path, monkeypatch, [{"type": "text", "text": "ok"}], **NO_GATE)
    await start(srv, tmp_path)
    return srv


async def _drop(server, text):
    return await call(server, "input.detect_drop", {"session_id": server.session.session_id, "text": text})


async def test_a_dropped_file_comes_back_as_an_at_path(server, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    res = await _drop(server, str(f))
    assert res["matched"] is True and res["text"] == f"@{f}" and res["is_image"] is False and res["name"] == "notes.txt"


async def test_a_dropped_image_is_flagged_but_still_an_at_path(server, tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG")
    res = await _drop(server, str(f))
    assert res["matched"] is True and res["text"] == f"@{f}" and res["is_image"] is True


async def test_quotes_escapes_and_file_uris_are_unwrapped(server, tmp_path):
    f = tmp_path / "my notes.txt"
    f.write_text("x")
    for typed in (f"'{f}'", f'"{f}"', str(f).replace(" ", "\\ "), f"file://{str(f).replace(' ', '%20')}"):
        res = await _drop(server, typed)
        assert res["matched"] is True and res["text"] == f'@"{f}"', typed  # a path with a space is quoted


async def test_a_relative_drop_resolves_against_the_session_directory(server, tmp_path):
    server.session.stored.cwd = str(tmp_path)
    (tmp_path / "rel.py").write_text("x")
    res = await _drop(server, "./rel.py")
    assert res["matched"] is True and res["text"] == f"@{tmp_path / 'rel.py'}"


async def test_text_that_is_not_one_existing_file_is_left_alone(server, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    for typed in (
        str(tmp_path / "missing.txt"),  # nothing there
        str(tmp_path),  # a directory is not a drop
        f"please read {f}",  # a prompt that mentions a file
        f"{f} {f}",  # two words
        "/help",
        "",
        "   ",
    ):
        assert (await _drop(server, typed))["matched"] is False, typed
    assert (await call(server, "input.detect_drop", {"text": 5}))["matched"] is False
    assert (await call(server, "input.detect_drop", {}))["matched"] is False


async def test_clipboard_paste_says_image_paste_is_unsupported(server):
    res = await call(server, "clipboard.paste", {"session_id": server.session.session_id})
    assert res["attached"] is False and "not supported" in res["message"]


def test_detect_drop_is_no_longer_an_unauthenticated_filesystem_probe():
    assert "input.detect_drop" not in gateway_server.OPEN_METHODS
