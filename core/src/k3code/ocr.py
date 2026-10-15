"""Offline OCR: text from screenshots, images and scanned PDFs, with a small local model.

The owner's models sit behind a gateway that may be offline, and a text-only model never sees an image. So an image
the agent reads is turned into text here, on the CPU, with no network: ``read`` of a ``.png`` returns the OCR text
with a header line, ``/ocr <path>`` and ``k3code ocr <path>`` print it. Engines, in order:

1. RapidOCR (the ``rapidocr`` package with ``onnxruntime``; the older ``rapidocr-onnxruntime`` is accepted too):
   PaddleOCR's detection + recognition models as ONNX, shipped inside the wheel, so nothing is downloaded after the
   install. Multilingual (Latin scripts, German umlauts included, plus Chinese and Japanese).
2. ``tesseract`` on PATH, with ``deu+eng`` when both languages are installed.
3. Neither: :class:`OcrUnavailable` with the install hint (:func:`install_hint`).

Every import here is lazy: the core never needs any of these at import time (``pyproject.toml``'s ``ocr`` extra adds
them; the installer's presetup installs that extra unless ``--minimal``). A PDF with a text layer is not OCR'd: its
text is extracted (pypdfium2, else ``pdftotext``); a scanned one is rendered page by page (pypdfium2, else
``pdftoppm``) and each page goes through the engine.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Files ``read`` hands to OCR instead of decoding as text. SVG is text and HEIC needs a decoder none of the engines
#: has, so neither is here.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
PDF_SUFFIXES = frozenset({".pdf"})
#: Pages of a PDF OCR'd per call unless asked otherwise (a page takes seconds on a CPU).
DEFAULT_MAX_PAGES = 20
#: A PDF page whose text layer has at least this many non-blank characters is read from the layer, not OCR'd.
TEXT_LAYER_MIN_CHARS = 20
#: pypdfium2 render scale (1 = 72 dpi): 2 gives 144 dpi, enough for body text and still fast.
PDF_RENDER_SCALE = 2
PDF_RENDER_DPI = 72 * PDF_RENDER_SCALE
#: Tesseract is asked for these languages, those of them that are installed; none installed means its default.
TESSERACT_LANGS = ("deu", "eng")
_SUBPROCESS_TIMEOUT = 300


class OcrUnavailable(RuntimeError):
    """No OCR engine can run here; ``str(exc)`` says how to enable one."""


@dataclass
class Block:
    """One recognised line or word group with the engine's confidence (0..1)."""

    text: str
    confidence: float


@dataclass
class OcrResult:
    text: str
    engine: str
    confidence: float
    blocks: list[Block] = field(default_factory=list)
    #: PDFs: pages read (OCR'd or extracted); an image is one page.
    pages: int = 1
    #: PDFs: pages past ``max_pages`` that were not read.
    skipped_pages: int = 0

    @property
    def header(self) -> str:
        """``OCR via rapidocr, 3 blocks, confidence 0.91`` (what ``read`` puts above the text)."""
        if self.engine == "pdf-text":
            n = f"{self.pages} page{'' if self.pages == 1 else 's'}"
            return f"Text layer of the PDF ({n}, no OCR needed)" + self._skipped()
        n = f"{len(self.blocks)} block{'' if len(self.blocks) == 1 else 's'}"
        pages = f", {self.pages} pages" if self.pages != 1 else ""
        return f"OCR via {self.engine}, {n}{pages}, confidence {self.confidence:.2f}" + self._skipped()

    def _skipped(self) -> str:
        if not self.skipped_pages:
            return ""
        return f" (first {self.pages} of {self.pages + self.skipped_pages} pages; pass max_pages for more)"

    def render(self) -> str:
        """Header line, then the text (or a note when nothing was found)."""
        body = self.text.strip("\n")
        return f"{self.header}\n{body}" if body else f"{self.header}\n(no text found)"


class Engine(Protocol):
    name: str

    def run(self, image: Path) -> list[Block]: ...


# ---- engines -----------------------------------------------------------------


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # a broken parent package, a namespace oddity
        return False


def rapidocr_available() -> str | None:
    """The importable RapidOCR distribution, else None: ``rapidocr`` needs ``onnxruntime`` next to it, the legacy
    ``rapidocr_onnxruntime`` wheel brings its own."""
    if _has_module("rapidocr") and _has_module("onnxruntime"):
        return "rapidocr"
    if _has_module("rapidocr_onnxruntime"):
        return "rapidocr_onnxruntime"
    return None


def rapidocr_version() -> str:
    import importlib.metadata as md

    for dist in ("rapidocr", "rapidocr-onnxruntime"):
        try:
            return md.version(dist)
        except md.PackageNotFoundError:
            continue
    return ""


def rapidocr_models() -> list[str]:
    """The ONNX model files shipped in the installed wheel (so doctor can show they are there, no download)."""
    which = rapidocr_available()
    if which is None:
        return []
    spec = importlib.util.find_spec(which)
    if spec is None or not spec.submodule_search_locations:
        return []
    root = Path(next(iter(spec.submodule_search_locations)))
    return sorted(p.name for p in root.rglob("*.onnx"))


class _RapidEngine:
    """Both RapidOCR APIs: 3.x returns an output object (``txts``/``scores``), 1.x a ``(list, elapse)`` pair."""

    name = "rapidocr"

    def __init__(self) -> None:
        which = rapidocr_available()
        if which is None:
            raise OcrUnavailable(install_hint())
        self._lock = threading.Lock()
        if which == "rapidocr":
            from rapidocr import RapidOCR  # type: ignore[import-not-found]

            # its logger writes coloured INFO lines to stderr (one per model it loads): the daemon log is not for that
            self._engine = RapidOCR(params={"Global.log_level": "error"})
            self._legacy = False
        else:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

            self._engine = RapidOCR()
            self._legacy = True

    def run(self, image: Path) -> list[Block]:
        with self._lock:  # one inference at a time: the models are shared by every session of the daemon
            out = self._engine(str(image))
        if self._legacy:
            rows, _elapse = out
            return [Block(str(row[1]), float(row[2])) for row in rows or []]
        txts = getattr(out, "txts", None) or ()
        scores = getattr(out, "scores", None) or ()
        return [Block(str(t), float(s)) for t, s in zip(txts, scores, strict=False)]


_rapid: _RapidEngine | None = None
_rapid_lock = threading.Lock()


def _rapid_engine() -> _RapidEngine:
    global _rapid
    with _rapid_lock:  # loading the models takes a second: once per process
        if _rapid is None:
            _rapid = _RapidEngine()
        return _rapid


def tesseract_binary() -> str | None:
    return shutil.which("tesseract")


def tesseract_languages(binary: str) -> list[str]:
    try:
        out = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    langs = []
    for line in (out.stdout + out.stderr).splitlines():
        word = line.strip()
        if word and " " not in word and word != "osd":
            langs.append(word)
    return langs


class _TesseractEngine:
    name = "tesseract"

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or tesseract_binary()
        if self.binary is None:
            raise OcrUnavailable(install_hint())
        installed = tesseract_languages(self.binary)
        self.langs = [lang for lang in TESSERACT_LANGS if lang in installed]

    def run(self, image: Path) -> list[Block]:
        cmd = [self.binary, str(image), "stdout"]
        if self.langs:
            cmd += ["-l", "+".join(self.langs)]
        cmd.append("tsv")
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT, check=False)
        if out.returncode != 0:
            raise RuntimeError(f"tesseract failed: {(out.stderr or out.stdout).strip()[:500]}")
        return parse_tesseract_tsv(out.stdout)


def parse_tesseract_tsv(tsv: str) -> list[Block]:
    """Tesseract's ``tsv`` output as one block per text line (its words joined), confidence the words' mean."""
    lines: dict[tuple[str, ...], list[tuple[str, float]]] = {}
    for raw in tsv.splitlines()[1:]:
        cols = raw.split("\t")
        if len(cols) < 12 or cols[0] != "5":  # level 5 = a word
            continue
        word = cols[11].strip()
        if not word:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            conf = -1.0
        key = (cols[1], cols[2], cols[3], cols[4])  # page, block, paragraph, line
        lines.setdefault(key, []).append((word, conf))
    blocks = []
    for words in lines.values():
        confs = [c for _w, c in words if c >= 0]
        mean = sum(confs) / len(confs) / 100 if confs else 0.0
        blocks.append(Block(" ".join(w for w, _c in words), mean))
    return blocks


def install_hint() -> str:
    """How to enable OCR here, for the tool error, the command and doctor."""
    uv = shutil.which("uv") or "uv"
    return (
        "no OCR engine is installed. Enable it with: re-run the installer without --minimal (it adds the ocr "
        f"extra), or `{uv} pip install --python {sys.executable} 'rapidocr>=3.10' onnxruntime pypdfium2` "
        "(`uv pip install 'k3code[ocr]'` from a checkout), or install tesseract (`apt install tesseract-ocr "
        "tesseract-ocr-deu`). OCR runs offline once installed."
    )


def select_engine() -> Engine:
    """The best engine that can run here, in the order of the module docstring."""
    if rapidocr_available():
        try:
            return _rapid_engine()
        except Exception as exc:  # noqa: BLE001 - a broken wheel (a missing libGL ...) must fall through
            logger.warning("rapidocr is installed but did not load (%s); trying tesseract", exc)
    if tesseract_binary():
        return _TesseractEngine()
    raise OcrUnavailable(install_hint())


def engine_status() -> dict[str, Any]:
    """What doctor shows: ``{"engine": name | None, "detail": ..., "pdf": renderer | None}``; nothing is loaded."""
    pdf = "pypdfium2" if _has_module("pypdfium2") else ("pdftoppm" if shutil.which("pdftoppm") else None)
    if (which := rapidocr_available()) is not None:
        models = rapidocr_models()
        ver = rapidocr_version()
        shipped = f"{len(models)} ONNX models in the wheel" if models else "models not found in the wheel"
        return {
            "engine": "rapidocr",
            "detail": f"rapidocr {ver} (offline, {shipped})".replace("  ", " "),
            "package": which,
            "pdf": pdf,
        }
    if (binary := tesseract_binary()) is not None:
        langs = tesseract_languages(binary)
        used = [lang for lang in TESSERACT_LANGS if lang in langs] or ["default language"]
        return {"engine": "tesseract", "detail": f"tesseract ({'+'.join(used)})", "package": binary, "pdf": pdf}
    return {"engine": None, "detail": "not installed", "package": None, "pdf": pdf}


# ---- images ------------------------------------------------------------------


def _result(blocks: list[Block], engine: str, *, pages: int = 1, skipped: int = 0) -> OcrResult:
    confs = [b.confidence for b in blocks]
    return OcrResult(
        text="\n".join(b.text for b in blocks),
        engine=engine,
        confidence=round(sum(confs) / len(confs), 4) if confs else 0.0,
        blocks=blocks,
        pages=pages,
        skipped_pages=skipped,
    )


def ocr_image(path: Path | str, *, engine: Engine | None = None) -> OcrResult:
    """The text in one image file. ``engine`` overrides the selection (tests, a caller that chose one)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    engine = engine or select_engine()
    return _result(engine.run(path), engine.name)


# ---- PDFs --------------------------------------------------------------------


def pdf_text_layer(path: Path, max_pages: int) -> tuple[list[str], int] | None:
    """The first ``max_pages`` pages' text layers and the page count, or None when nothing can read a PDF here."""
    if _has_module("pypdfium2"):
        import pypdfium2 as pdfium  # type: ignore[import-not-found]

        doc = pdfium.PdfDocument(str(path))
        try:
            total = len(doc)
            texts = []
            for i in range(min(total, max_pages)):
                page = doc[i]
                try:
                    texts.append(page.get_textpage().get_text_range())
                finally:
                    page.close()
            return texts, total
        finally:
            doc.close()
    if shutil.which("pdftotext"):
        out = subprocess.run(
            ["pdftotext", "-layout", "-l", str(max_pages), str(path), "-"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
        if out.returncode != 0:
            raise RuntimeError(f"pdftotext failed: {out.stderr.strip()[:500]}")
        pages = out.stdout.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        total = len(pages)
        if shutil.which("pdfinfo"):
            info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=60, check=False)
            for line in info.stdout.splitlines():
                if line.startswith("Pages:"):
                    total = int(line.split()[1])
        return pages, total
    return None


def render_pdf_pages(path: Path, out_dir: Path, max_pages: int) -> list[Path]:
    """PNG files of the first ``max_pages`` pages (pypdfium2, else ``pdftoppm``); OcrUnavailable with neither."""
    if _has_module("pypdfium2") and _has_module("PIL"):
        import pypdfium2 as pdfium  # type: ignore[import-not-found]

        doc = pdfium.PdfDocument(str(path))
        try:
            files = []
            for i in range(min(len(doc), max_pages)):
                page = doc[i]
                try:
                    target = out_dir / f"page-{i + 1:04d}.png"
                    page.render(scale=PDF_RENDER_SCALE).to_pil().save(target)
                    files.append(target)
                finally:
                    page.close()
            return files
        finally:
            doc.close()
    if shutil.which("pdftoppm"):
        out = subprocess.run(
            ["pdftoppm", "-r", str(PDF_RENDER_DPI), "-l", str(max_pages), "-png", str(path), str(out_dir / "page")],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
        if out.returncode != 0:
            raise RuntimeError(f"pdftoppm failed: {out.stderr.strip()[:500]}")
        return sorted(out_dir.glob("page*.png"))
    raise OcrUnavailable(
        "no PDF renderer is installed: pypdfium2 (part of the ocr extra, see the install hint) or poppler's pdftoppm "
        "(`apt install poppler-utils`) is needed to OCR a scanned PDF"
    )


def ocr_pdf(
    path: Path | str,
    max_pages: int = DEFAULT_MAX_PAGES,
    *,
    engine: Engine | None = None,
    renderer: Callable[[Path, Path, int], list[Path]] | None = None,
) -> OcrResult:
    """Text of a PDF: its text layer when it has one (no engine needed), else OCR of its rendered pages.

    Pages are decided one by one: a scan with a few OCR'd pages mixed in gets both. ``renderer`` replaces
    :func:`render_pdf_pages` (tests)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    max_pages = max(1, int(max_pages))
    layer = pdf_text_layer(path, max_pages)
    if layer is None:
        raise OcrUnavailable(
            "nothing here can open a PDF: install pypdfium2 (part of the ocr extra) or poppler-utils (pdftotext, "
            "pdftoppm)"
        )
    texts, total = layer
    read = min(total, max_pages)
    skipped = max(0, total - read)
    has_layer = [len(t.strip()) >= TEXT_LAYER_MIN_CHARS for t in texts]
    if all(has_layer) and texts:
        text = "\n\n".join(f"[page {i + 1}]\n{t.strip()}" for i, t in enumerate(texts))
        return OcrResult(text=text, engine="pdf-text", confidence=1.0, pages=read, skipped_pages=skipped)
    engine = engine or select_engine()
    render = renderer or render_pdf_pages
    blocks: list[Block] = []
    parts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="k3code-ocr-") as tmp:
        images = render(path, Path(tmp), read)
        for i in range(read):
            if has_layer[i]:
                parts.append(f"[page {i + 1}]\n{texts[i].strip()}")
                continue
            if i >= len(images):
                parts.append(f"[page {i + 1}]\n(page could not be rendered)")
                continue
            page_blocks = engine.run(images[i])
            blocks += page_blocks
            parts.append(f"[page {i + 1}]\n" + ("\n".join(b.text for b in page_blocks) or "(no text found)"))
    result = _result(blocks, engine.name, pages=read, skipped=skipped)
    result.text = "\n\n".join(parts)
    return result


def ocr_file(path: Path | str, max_pages: int = DEFAULT_MAX_PAGES, *, engine: Engine | None = None) -> OcrResult:
    """``ocr_pdf`` for a PDF, ``ocr_image`` for anything else (the engine decides whether it can decode it)."""
    path = Path(path)
    if path.suffix.lower() in PDF_SUFFIXES:
        return ocr_pdf(path, max_pages, engine=engine)
    return ocr_image(path, engine=engine)


def is_ocr_candidate(path: Path | str) -> bool:
    """``read`` sends these to OCR: image files and PDFs by suffix (a text file named ``.png`` is not a thing)."""
    suffix = Path(path).suffix.lower()
    return suffix in IMAGE_SUFFIXES or suffix in PDF_SUFFIXES


def _reset_for_tests() -> None:
    global _rapid
    _rapid = None


__all__ = [
    "DEFAULT_MAX_PAGES",
    "IMAGE_SUFFIXES",
    "PDF_SUFFIXES",
    "Block",
    "Engine",
    "OcrResult",
    "OcrUnavailable",
    "engine_status",
    "install_hint",
    "is_ocr_candidate",
    "ocr_file",
    "ocr_image",
    "ocr_pdf",
    "select_engine",
]
