"""What a failed tool call looks like, stable across runs: an error class and a normalised signature.

The loop guard compares signatures to notice the same failure again and again within a turn; the learning hub counts
them per project to propose a gotcha. A signature is the first line of the error with paths, numbers, hex ids and
uuids replaced by placeholders, scrubbed of credentials and cut to SIGNATURE_CHARS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from k3code.redact import scrub_text

SIGNATURE_CHARS = 160

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PATH = re.compile(r"(?:[A-Za-z0-9_.~\-]*/)+[A-Za-z0-9_.\-]*")
_HEX = re.compile(r"\b(?:0x)?(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}\b")
_NUMBER = re.compile(r"(?<![A-Za-z_<])\d+(?:\.\d+)?")
_EXCEPTION = re.compile(r"^Tool execution failed: ([A-Za-z_][\w.]*):")


@dataclass(frozen=True)
class Failure:
    error_class: str  # "exit 127", "invalid_arguments", "KeyError", "timeout", "denied", "File not found", ...
    signature: str  # normalised first line (the bash program and exit code lead it)
    first_line: str  # the raw first line, scrubbed (what a person reads)


def first_line(text: str) -> str:
    return next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")


def normalise(line: str) -> str:
    line = _UUID.sub("<uuid>", line)
    line = _PATH.sub("<path>", line)
    line = _HEX.sub("<hex>", line)
    line = _NUMBER.sub("<n>", line)
    line = re.sub(r"\s+", " ", scrub_text(line)).strip()
    return line[:SIGNATURE_CHARS]


def _program(command: str) -> str:
    words = command.split()
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words.pop(0)
    return words[0].rsplit("/", 1)[-1] if words else ""


def _error_class(text: str) -> str:
    low = text.lower()
    if low.startswith("invalid arguments"):
        return "invalid_arguments"
    if m := _EXCEPTION.match(text):
        return m.group(1)
    if "timed out" in low:
        return "timeout"
    if "denied" in low or low.startswith("permission"):
        return "denied"
    head = text.split(":", 1)[0].strip()
    return head[:40] if head and len(head) <= 40 else "error"


def failure_of(tool: str, args: dict[str, Any] | None, result: dict[str, Any]) -> Failure | None:
    """The failure in ``result``, or None when the call succeeded.

    A failure is an error result, or a bash command that exited non-zero (its signature then leads with the program
    and the exit code, then the first line of stderr).
    """
    if "error" in result and "content" not in result:
        line = first_line(result["error"])
        cls = _error_class(line)
        if tool == "bash" and cls == "timeout":
            line = f"{_program(str((args or {}).get('command', '')))}: {line}"
        return Failure(cls, normalise(line), scrub_text(line)[:SIGNATURE_CHARS])
    code = result.get("exit_code")
    if tool == "bash" and code not in (0, None):
        err = first_line(result.get("stderr") or "")
        line = f"{_program(str((args or {}).get('command', '')))}: exit {code}" + (f": {err}" if err else "")
        return Failure(f"exit {code}", normalise(line), scrub_text(line)[:SIGNATURE_CHARS])
    return None


def describe_call(tool: str, args: dict[str, Any] | None) -> str:
    """``bash `npm test` `` / ``read src/x.py``: the call in a note or a list, scrubbed, one line."""
    args = args or {}
    if tool == "bash":
        what = first_line(str(args.get("command", "")))[:120]
        return f"bash `{scrub_text(what)}`"
    target = args.get("path") or args.get("pattern") or args.get("url") or ""
    return f"{tool} {scrub_text(str(target))[:120]}".strip()
