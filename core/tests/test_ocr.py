"""Offline OCR (k3code.ocr): engine selection, the error without an engine, the PDF text-layer short-cut, the read
tool, /ocr, `k3code ocr` and the doctor line. Engines are fakes: no model runs and nothing is downloaded, except the
one real smoke test at the end, which is skipped when rapidocr is not installed."""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import types
import zlib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from k3code import doctor, ocr
from k3code.cli import cli
from k3code.commands.builtin import build_registry as build_commands
from k3code.commands.ocr_cmd import OcrCommand
from k3code.permissions.state import PermissionState
from k3code.tools import build_registry, format_tool_result, tool_read

# ---- fixtures ------------------------------------------------------------------


def tiny_png(path: Path, width: int = 4, height: int = 4) -> Path:
    """A valid white PNG written from bytes (no PIL needed)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    return path


def minimal_pdf(path: Path, text: str | None) -> Path:
    """A one-page PDF: with ``text`` it has a text layer (Helvetica), without one the page is empty (a "scan")."""
    stream = f"BT /F1 24 Tf 40 100 Td ({text}) Tj ET".encode() if text else b""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offsets:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


class FakeEngine:
    name = "fake"

    def __init__(self, blocks: list[ocr.Block] | None = None) -> None:
        self.blocks = blocks if blocks is not None else [ocr.Block("Hello Zürich", 0.95), ocr.Block("Nr 1234", 0.87)]
        self.calls: list[Path] = []

    def run(self, image: Path) -> list[ocr.Block]:
        self.calls.append(image)
        return list(self.blocks)


@pytest.fixture
def no_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither rapidocr nor tesseract nor a PDF library: the state of a --minimal install on a bare machine."""
    monkeypatch.setattr(ocr, "rapidocr_available", lambda: None)
    monkeypatch.setattr(ocr, "tesseract_binary", lambda: None)
    monkeypatch.setattr(ocr, "_has_module", lambda name: False)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    ocr._reset_for_tests()


@pytest.fixture
def fake_rapid(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    """select_engine() returns this fake as "rapidocr": the read tool and the commands run with no model."""
    engine = FakeEngine()
    engine.name = "rapidocr"
    monkeypatch.setattr(ocr, "select_engine", lambda: engine)
    return engine


# ---- engine selection -------------------------------------------------------------


def test_nothing_installed_is_a_clear_error_with_the_install_hint(no_engines: None, tmp_path: Path) -> None:
    with pytest.raises(ocr.OcrUnavailable) as exc:
        ocr.ocr_image(tiny_png(tmp_path / "shot.png"))
    msg = str(exc.value)
    assert "no OCR engine is installed" in msg
    assert "--minimal" in msg and "rapidocr" in msg and "tesseract" in msg
    assert ocr.engine_status()["engine"] is None


def test_rapidocr_is_preferred_over_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    rapid = FakeEngine()
    monkeypatch.setattr(ocr, "rapidocr_available", lambda: "rapidocr")
    monkeypatch.setattr(ocr, "_rapid_engine", lambda: rapid)
    monkeypatch.setattr(ocr, "tesseract_binary", lambda: "/usr/bin/tesseract")
    assert ocr.select_engine() is rapid


def test_a_broken_rapidocr_falls_through_to_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise ImportError("libGL.so.1: cannot open shared object file")

    monkeypatch.setattr(ocr, "rapidocr_available", lambda: "rapidocr")
    monkeypatch.setattr(ocr, "_rapid_engine", boom)
    monkeypatch.setattr(ocr, "tesseract_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(ocr, "tesseract_languages", lambda binary: ["eng", "deu", "osd"])
    engine = ocr.select_engine()
    assert isinstance(engine, ocr._TesseractEngine)
    assert engine.langs == ["deu", "eng"]  # both installed: German first


def test_tesseract_without_the_languages_uses_its_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "rapidocr_available", lambda: None)
    monkeypatch.setattr(ocr, "tesseract_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(ocr, "tesseract_languages", lambda binary: ["fra"])
    engine = ocr.select_engine()
    assert isinstance(engine, ocr._TesseractEngine) and engine.langs == []
    status = ocr.engine_status()
    assert status["engine"] == "tesseract" and "default language" in status["detail"]


def test_tesseract_tsv_is_grouped_into_lines() -> None:
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t480\t140\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t23\t27\t86\t27\t96.7\tHello\n"
        "5\t1\t1\t1\t1\t2\t125\t27\t110\t27\t86.9\tZurich\n"
        "4\t1\t1\t1\t2\t0\t23\t82\t331\t34\t-1\t\n"
        "5\t1\t1\t1\t2\t1\t23\t82\t174\t34\t91.4\tRechnung\n"
        "5\t1\t1\t1\t2\t2\t214\t83\t39\t26\t \n"  # a short row: ignored
    )
    blocks = ocr.parse_tesseract_tsv(tsv)
    assert [b.text for b in blocks] == ["Hello Zurich", "Rechnung"]
    assert blocks[0].confidence == pytest.approx(0.918)


def test_ocr_image_uses_the_given_engine_and_averages_confidence(tmp_path: Path) -> None:
    engine = FakeEngine()
    res = ocr.ocr_image(tiny_png(tmp_path / "a.png"), engine=engine)
    assert engine.calls == [tmp_path / "a.png"]
    assert res.text == "Hello Zürich\nNr 1234" and res.engine == "fake"
    assert res.confidence == pytest.approx(0.91)
    assert res.header == "OCR via fake, 2 blocks, confidence 0.91"
    assert res.render().startswith("OCR via fake, 2 blocks, confidence 0.91\nHello Zürich")
    empty = ocr.ocr_image(tmp_path / "a.png", engine=FakeEngine([]))
    assert empty.confidence == 0.0 and empty.render().endswith("(no text found)")
    with pytest.raises(FileNotFoundError):
        ocr.ocr_image(tmp_path / "missing.png", engine=engine)


def test_candidates_are_images_and_pdfs_by_suffix() -> None:
    assert ocr.is_ocr_candidate("shot.PNG") and ocr.is_ocr_candidate("scan.pdf") and ocr.is_ocr_candidate("x.tiff")
    assert not ocr.is_ocr_candidate("main.py") and not ocr.is_ocr_candidate("logo.svg")


# ---- PDFs ------------------------------------------------------------------------


def test_pdf_with_a_text_layer_is_extracted_not_ocrd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "pdf_text_layer", lambda path, n: (["Hello text layer page one here"], 1))
    engine = FakeEngine()
    res = ocr.ocr_pdf(minimal_pdf(tmp_path / "text.pdf", "Hello text layer page one here"), engine=engine)
    assert res.engine == "pdf-text" and res.confidence == 1.0
    assert engine.calls == []  # no OCR ran
    assert "Hello text layer page one here" in res.text
    assert res.header == "Text layer of the PDF (1 page, no OCR needed)"


def test_scanned_pdf_is_rendered_and_each_page_ocrd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "pdf_text_layer", lambda path, n: (["", "   \n"], 2))
    rendered: list[tuple[Path, int]] = []

    def renderer(path: Path, out: Path, max_pages: int) -> list[Path]:
        rendered.append((path, max_pages))
        return [tiny_png(out / f"page-{i}.png") for i in range(max_pages)]

    engine = FakeEngine()
    res = ocr.ocr_pdf(minimal_pdf(tmp_path / "scan.pdf", None), engine=engine, renderer=renderer)
    assert rendered == [(tmp_path / "scan.pdf", 2)]
    assert len(engine.calls) == 2 and res.pages == 2 and res.skipped_pages == 0
    assert res.header == "OCR via fake, 4 blocks, 2 pages, confidence 0.91"
    assert res.text.startswith("[page 1]\nHello Zürich\nNr 1234\n\n[page 2]\n")


def test_mixed_pdf_keeps_text_pages_and_ocrs_the_rest_up_to_max_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "pdf_text_layer", lambda path, n: (["A page with a real text layer", ""][:n], 5))
    engine = FakeEngine()
    renderer = lambda path, out, n: [tiny_png(out / f"p{i}.png") for i in range(n)]  # noqa: E731
    res = ocr.ocr_pdf(minimal_pdf(tmp_path / "mix.pdf", None), max_pages=2, engine=engine, renderer=renderer)
    assert len(engine.calls) == 1  # only the page without a layer
    assert res.pages == 2 and res.skipped_pages == 3
    assert "[page 1]\nA page with a real text layer" in res.text and "[page 2]\nHello Zürich" in res.text
    assert "first 2 of 5 pages; pass max_pages for more" in res.header


def test_pdf_without_any_reader_says_what_to_install(no_engines: None, tmp_path: Path) -> None:
    with pytest.raises(ocr.OcrUnavailable, match="pypdfium2"):
        ocr.ocr_pdf(minimal_pdf(tmp_path / "x.pdf", "text"))


@pytest.mark.skipif(importlib.util.find_spec("pypdfium2") is None, reason="pypdfium2 is not installed")
def test_real_pypdfium2_reads_the_text_layer_and_renders_an_empty_page(tmp_path: Path) -> None:
    texts, total = ocr.pdf_text_layer(minimal_pdf(tmp_path / "t.pdf", "Hello text layer page one here"), 5)
    assert total == 1 and "Hello text layer" in texts[0]
    engine = FakeEngine()
    res = ocr.ocr_pdf(tmp_path / "t.pdf", engine=engine)
    assert res.engine == "pdf-text" and engine.calls == []
    if importlib.util.find_spec("PIL") is not None:
        files = ocr.render_pdf_pages(minimal_pdf(tmp_path / "s.pdf", None), tmp_path, 3)
        assert len(files) == 1 and files[0].read_bytes()[:4] == b"\x89PNG"


# ---- the read tool -----------------------------------------------------------------


async def test_read_of_an_image_returns_the_ocr_text_with_a_header(fake_rapid: FakeEngine, tmp_path: Path) -> None:
    tiny_png(tmp_path / "shot.png")
    result = await tool_read({"path": "shot.png"}, cwd=tmp_path)
    assert "error" not in result
    assert result["ocr"] == {"engine": "rapidocr", "confidence": 0.91, "pages": 1}
    shown = format_tool_result(result)
    assert shown == "OCR via rapidocr, 2 blocks, confidence 0.91\nHello Zürich\nNr 1234"
    assert "\t" not in shown  # no line numbers: the text is not a file the model could edit
    assert fake_rapid.calls == [tmp_path / "shot.png"]


async def test_read_of_an_image_without_an_engine_is_an_error_that_says_how(no_engines: None, tmp_path: Path) -> None:
    tiny_png(tmp_path / "shot.png")
    result = await tool_read({"path": str(tmp_path / "shot.png")})
    assert set(result) == {"error"}
    assert "cannot read shot.png: no OCR engine is installed" in result["error"]
    assert "--minimal" in result["error"]


async def test_read_of_a_text_pdf_needs_no_engine(no_engines: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ocr, "pdf_text_layer", lambda path, n: (["Invoice 2026-10 total 42.00 CHF"], 1))
    result = await tool_read({"path": str(minimal_pdf(tmp_path / "inv.pdf", "Invoice"))})
    assert result["ocr"]["engine"] == "pdf-text"
    assert format_tool_result(result).startswith("Text layer of the PDF (1 page, no OCR needed)\n[page 1]\nInvoice")


async def test_read_of_a_text_file_is_unchanged(fake_rapid: FakeEngine, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    result = await tool_read({"path": str(tmp_path / "a.py")})
    assert result["content"] == "x = 1" and "first" in result and fake_rapid.calls == []


def test_read_tool_description_mentions_images() -> None:
    spec, _handler = build_registry().get("read")
    assert "OCR" in spec.description and "PDF" in spec.description


# ---- /ocr and k3code ocr ------------------------------------------------------------


def _ctx(tmp_path: Path, perms: PermissionState | None = None) -> types.SimpleNamespace:
    live = types.SimpleNamespace(stored=types.SimpleNamespace(cwd=str(tmp_path)), perms=perms)
    return types.SimpleNamespace(sessions={"s1": live}, config=None, commands=build_commands())


async def test_slash_ocr_prints_the_text(fake_rapid: FakeEngine, tmp_path: Path) -> None:
    tiny_png(tmp_path / "shot.png")
    out = await OcrCommand().handle(_ctx(tmp_path), "s1", "shot.png")
    assert out["type"] == "message" and out["engine"] == "rapidocr"
    assert out["output"] == "OCR via rapidocr, 2 blocks, confidence 0.91\nHello Zürich\nNr 1234"
    assert (await OcrCommand().handle(_ctx(tmp_path), "s1", ""))["output"].startswith("Usage: /ocr")
    assert (await OcrCommand().handle(_ctx(tmp_path), "s1", "--pages x shot.png"))["output"].startswith("Usage:")
    assert "File not found" in (await OcrCommand().handle(_ctx(tmp_path), "s1", "nope.png"))["output"]
    assert build_commands().get("ocr") is not None


async def test_slash_ocr_without_an_engine_reports_the_hint(no_engines: None, tmp_path: Path) -> None:
    tiny_png(tmp_path / "shot.png")
    out = await OcrCommand().handle(_ctx(tmp_path), "s1", str(tmp_path / "shot.png"))
    assert out["output"].startswith("/ocr cannot run: no OCR engine is installed")


async def test_slash_ocr_respects_the_permission_engine(fake_rapid: FakeEngine, tmp_path: Path) -> None:
    """Reading a file for OCR is a read: a credential path is refused like `read` refuses it; a path outside the
    project (an ask for the model) runs, since the person typed it."""
    perms = PermissionState(cwd=tmp_path)
    perms.loaded = True  # no config on disk: builtin rules only
    secret = tiny_png(tmp_path / ".env.png")  # a dotenv name: the sensitive-path hardline
    out = await OcrCommand().handle(_ctx(tmp_path, perms), "s1", str(secret))
    assert out["output"].startswith("Refused:") and "sensitive-path" in out["output"]
    assert fake_rapid.calls == []
    outside = tiny_png(tmp_path.parent / f"{tmp_path.name}-outside.png")
    try:
        out = await OcrCommand().handle(_ctx(tmp_path, perms), "s1", str(outside))
        assert out["output"].startswith("OCR via rapidocr")
    finally:
        outside.unlink()
    # headless (`k3code -p "/ocr x"`): no session, so only the credential check applies
    light = types.SimpleNamespace(config=None, commands=build_commands())
    out = await OcrCommand().handle(light, None, str(secret))
    assert out["output"].startswith("Refused:")


def test_cli_ocr_subcommand(fake_rapid: FakeEngine, tmp_path: Path) -> None:
    shot = tiny_png(tmp_path / "shot.png")
    r = CliRunner().invoke(cli, ["ocr", str(shot)])
    assert r.exit_code == 0, r.output
    assert r.output == "OCR via rapidocr, 2 blocks, confidence 0.91\nHello Zürich\nNr 1234\n"
    r = CliRunner().invoke(cli, ["ocr", "--json", str(shot)])
    data = json.loads(r.output)
    assert data["engine"] == "rapidocr" and data["blocks"][0] == {"text": "Hello Zürich", "confidence": 0.95}


def test_cli_ocr_without_an_engine_exits_nonzero(no_engines: None, tmp_path: Path) -> None:
    r = CliRunner().invoke(cli, ["ocr", str(tiny_png(tmp_path / "shot.png"))])
    assert r.exit_code == 1 and "no OCR engine is installed" in r.output


# ---- doctor ----------------------------------------------------------------------


def test_doctor_ocr_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ocr,
        "engine_status",
        lambda: {
            "engine": "rapidocr",
            "detail": "rapidocr 3.10.0 (offline, 3 ONNX models in the wheel)",
            "pdf": "pypdfium2",
        },
    )
    chk = doctor.check_ocr()
    assert chk.status == doctor.OK and chk.detail.startswith("rapidocr 3.10.0 (offline")
    assert "PDF via pypdfium2" in chk.detail
    monkeypatch.setattr(ocr, "engine_status", lambda: {"engine": "tesseract", "detail": "tesseract (eng)", "pdf": None})
    chk = doctor.check_ocr()
    assert chk.status == doctor.OK and "tesseract (eng)" in chk.detail and "no PDF renderer" in chk.detail
    assert "--minimal" in chk.fix
    monkeypatch.setattr(ocr, "engine_status", lambda: {"engine": None, "detail": "not installed", "pdf": None})
    chk = doctor.check_ocr()
    assert chk.status == doctor.WARN and chk.detail.startswith("not installed")
    assert "re-run the installer without --minimal" in chk.fix and "rapidocr" in chk.fix
    assert "ocr" in {c.name for c in doctor.install_subset()}


# ---- the real engine, when it is here -------------------------------------------------


@pytest.mark.skipif(
    ocr.rapidocr_available() is None or importlib.util.find_spec("PIL") is None,
    reason="rapidocr (with onnxruntime) and Pillow are not both installed",
)
def test_real_rapidocr_reads_rendered_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundled model on an image rendered here (no download: the proxy variables point nowhere)."""
    from PIL import Image, ImageDraw, ImageFont

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    img = Image.new("RGB", (480, 90), "white")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 36)
    except OSError:
        font = ImageFont.load_default(size=36)
    ImageDraw.Draw(img).text((20, 20), "Rechnung Nr 1234", fill="black", font=font)
    img.save(tmp_path / "rendered.png")
    ocr._reset_for_tests()
    res = ocr.ocr_image(tmp_path / "rendered.png")
    assert res.engine == "rapidocr" and res.confidence > 0.8
    assert "1234" in res.text.replace(" ", "") and "Rechnung" in res.text.replace(" ", "")
