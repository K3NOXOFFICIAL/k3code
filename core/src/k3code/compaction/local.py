"""The small local summary model (tier 2): a GGUF under ``$K3CODE_HOME/models/`` run through llama-cpp-python.

The llama import is lazy and the package optional (``pip install 'k3code[local]'``): the gateway never imports it
unless a model file is present and compaction reaches this tier. :class:`LocalSummarizer` is the seam; tests use
:class:`FakeLocalSummarizer`. ``k3code models fetch`` downloads a model from :data:`MODELS` and checks its sha256.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from k3code.paths import ensure_private_dir, home

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    file: str
    url: str
    size: int
    sha256: str
    note: str


#: Built-in models. Sizes and sha256 are the Hugging Face LFS values (api/models/<repo>/paths-info, 2026-10-15).
MODELS: dict[str, ModelSpec] = {
    "qwen2.5-1.5b": ModelSpec(
        name="qwen2.5-1.5b",
        file="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        url="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        size=1_117_320_736,
        sha256="6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
        note="default; about 1.1 GB on disk, 2 GB of RAM while it runs",
    ),
    "qwen2.5-0.5b": ModelSpec(
        name="qwen2.5-0.5b",
        file="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        url="https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        size=491_400_032,
        sha256="74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db",
        note="for weaker machines; about 0.5 GB on disk, worse summaries",
    ),
}
DEFAULT_MODEL = "qwen2.5-1.5b"
#: ``compaction.local_model`` may be a name from MODELS, a file name under models/, or a path to a GGUF.
COMPACTION_DEFAULTS: dict[str, Any] = {"local_model": "", "local_threads": 0, "prefer_local": False}
#: Context the local model is given (tokens); the folded text is cut to fit it (about 4 chars per token).
LOCAL_CONTEXT_TOKENS = 8192
LOCAL_INPUT_CHARS = 24_000
LOCAL_SUMMARY_TOKENS = 600
LOCAL_SYSTEM = (
    "You summarize a coding conversation for the assistant that continues it. State the task, the decisions, what "
    "was done (files, commands, results), and what is still open. Plain facts, under 300 words, no preamble."
)


def models_dir() -> Path:
    return home() / "models"


def compaction_settings(config: Any) -> dict[str, Any]:
    return {**COMPACTION_DEFAULTS, **dict(getattr(config, "compaction", None) or {})}


def model_name_of(path: Path) -> str:
    """The built-in name of a model file, else its stem (for the compaction event's label)."""
    for spec in MODELS.values():
        if spec.file == path.name:
            return spec.name
    return path.stem


def resolve_model(config: Any) -> Path | None:
    """The GGUF file ``compaction.local_model`` names when it exists; with the key empty, the default model when it
    was fetched, else the first built-in model present. None when there is no model file."""
    setting = str(compaction_settings(config)["local_model"] or "").strip()
    folder = models_dir()
    if setting:
        if setting in MODELS:
            candidate = folder / MODELS[setting].file
        else:
            candidate = Path(setting).expanduser()
            if not candidate.is_absolute() and "/" not in setting:
                candidate = folder / setting
        return candidate if candidate.is_file() else None
    for name in (DEFAULT_MODEL, *MODELS):
        candidate = folder / MODELS[name].file
        if candidate.is_file():
            return candidate
    return None


def llama_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("llama_cpp") is not None


def status(config: Any) -> dict[str, Any]:
    """What `k3code doctor` reports: the model file (or None), whether llama-cpp-python is importable, prefer_local."""
    path = resolve_model(config)
    return {
        "model": str(path) if path else None,
        "name": model_name_of(path) if path else None,
        "llama": llama_available(),
        "prefer_local": bool(compaction_settings(config)["prefer_local"]),
    }


class LocalSummarizer(Protocol):
    """One summary call on the local model."""

    #: Label for the compaction event: ``local qwen2.5-1.5b``.
    label: str

    async def summarize(self, text: str) -> str: ...


class FakeLocalSummarizer:
    """Tests: a scripted summary, or a failure when ``error`` is set; records what it was asked."""

    def __init__(self, reply: str = "local summary", *, label: str = "local fake", error: str = "") -> None:
        self.reply, self.label, self.error = reply, label, error
        self.calls: list[str] = []

    async def summarize(self, text: str) -> str:
        self.calls.append(text)
        if self.error:
            raise RuntimeError(self.error)
        return self.reply


class LlamaSummarizer:
    """llama-cpp-python on a GGUF file. The model loads on the first call (a few seconds, ~2 GB RAM for the 1.5B)
    and stays loaded; ``threads`` 0 leaves the choice to llama.cpp (half the cores)."""

    def __init__(self, path: Path, *, threads: int = 0) -> None:
        self.path = Path(path)
        self.threads = int(threads or 0)
        self.label = f"local {model_name_of(self.path)}"
        self._llm: Any = None

    def _load(self) -> Any:
        if self._llm is None:
            from llama_cpp import Llama  # optional dependency; only imported here

            self._llm = Llama(
                model_path=str(self.path),
                n_ctx=LOCAL_CONTEXT_TOKENS,
                n_threads=self.threads or None,
                verbose=False,
            )
        return self._llm

    def _complete(self, text: str) -> str:
        llm = self._load()
        res = llm.create_chat_completion(
            messages=[{"role": "system", "content": LOCAL_SYSTEM}, {"role": "user", "content": text}],
            max_tokens=LOCAL_SUMMARY_TOKENS,
            temperature=0.2,
        )
        return str(res["choices"][0]["message"]["content"] or "")

    async def summarize(self, text: str) -> str:
        import asyncio

        return await asyncio.to_thread(self._complete, text[:LOCAL_INPUT_CHARS])


def build_summarizer(config: Any) -> LocalSummarizer | None:
    """A summarizer for the configured model, or None when there is no model file or no llama-cpp-python."""
    path = resolve_model(config)
    if path is None or not llama_available():
        return None
    return LlamaSummarizer(path, threads=int(compaction_settings(config)["local_threads"] or 0))


# ---- fetching ------------------------------------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def present(name: str) -> Path | None:
    spec = MODELS[name]
    path = models_dir() / spec.file
    return path if path.is_file() else None


def verify(name: str) -> tuple[bool, str]:
    """Whether the stored file matches the table (size and sha256); the checksum is cached next to the file."""
    spec = MODELS[name]
    path = models_dir() / spec.file
    if not path.is_file():
        return False, "not fetched"
    if path.stat().st_size != spec.size:
        return False, f"size {path.stat().st_size} differs from the expected {spec.size}"
    cached = _read_sum(path)
    digest = cached or sha256_of(path)
    if digest != spec.sha256:
        return False, "sha256 does not match the built-in table"
    if not cached:
        _write_sum(path, digest)
    return True, "ok"


def _sum_path(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _read_sum(path: Path) -> str:
    try:
        data = json.loads(_sum_path(path).read_text(encoding="utf-8"))
        if int(data.get("size", -1)) == path.stat().st_size and int(data.get("mtime", -1)) == int(path.stat().st_mtime):
            return str(data.get("sha256") or "")
    except (OSError, ValueError):
        pass
    return ""


def _write_sum(path: Path, digest: str) -> None:
    st = path.stat()
    _sum_path(path).write_text(
        json.dumps({"sha256": digest, "size": st.st_size, "mtime": int(st.st_mtime)}) + "\n", encoding="utf-8"
    )


Downloader = Callable[[str, Path, Callable[[int, int], None] | None], None]


def _download(url: str, dest: Path, progress: Callable[[int, int], None] | None) -> None:
    """Stream ``url`` to ``dest`` with httpx (follows Hugging Face's CDN redirect; uses the environment's proxy)."""
    import httpx

    with (
        httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client,
        client.stream("GET", url) as res,
    ):
        res.raise_for_status()
        total = int(res.headers.get("content-length") or 0)
        done = 0
        with dest.open("wb") as f:
            for chunk in res.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)


def fetch(
    name: str = DEFAULT_MODEL,
    *,
    download: Downloader = _download,
    progress: Callable[[int, int], None] | None = None,
    force: bool = False,
) -> Path:
    """Download ``name`` into models/ and verify it; a verified file already there is kept. Raises on a mismatch
    (the bad file is removed) and on an unknown name."""
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}; known: {', '.join(MODELS)}")
    spec = MODELS[name]
    folder = ensure_private_dir(models_dir())
    path = folder / spec.file
    if path.is_file() and not force:
        ok, why = verify(name)
        if ok:
            return path
        logger.warning("%s: %s; fetching again", path, why)
    part = path.with_name(path.name + ".part")
    try:
        download(spec.url, part, progress)
        size = part.stat().st_size
        if size != spec.size:
            raise RuntimeError(f"downloaded {size} bytes, expected {spec.size}")
        digest = sha256_of(part)
        if digest != spec.sha256:
            raise RuntimeError("sha256 of the download does not match the built-in table")
        os.replace(part, path)
    finally:
        if part.exists():
            part.unlink()
    _write_sum(path, digest)
    return path
