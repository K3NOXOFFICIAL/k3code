"""``ask_user_question``: the model asks the user 1-4 multiple-choice questions in the TUI's clarify overlay.

Modeled on Claude Code's AskUserQuestion. Registered on the gateway's main loop only (``GatewayServer._build_loop``),
never in ``build_registry()``: the headless CLI, the planner and subagents have nobody to ask. An unattended run
(background, cron, loop, automation, goal continuation) is refused at call time instead of waiting out the approval
timeout.

Wire (server→client ``clarify`` request, batch form)::

    {"question": "<q1 text>", "questions": [{"qid": "q1", "question", "header", "choices": [labels],
     "descriptions": [same length], "multi_select": bool}, ...]}

The client answers ``{"answers": {qid: str}}`` (a multi-select answer is a JSON array string), optionally with
``"chat": true``; ``{}`` or ``{"answer": ""}`` cancels; a lone ``{"answer": "x"}`` (an older TUI) answers q1.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from k3code.providers.types import ToolSpec

NAME = "ask_user_question"
MAX_QUESTIONS = 4
MIN_OPTIONS, MAX_OPTIONS = 2, 4
MAX_HEADER = 12  # display cells, not characters (a CJK character is two)
MAX_QUESTION, MAX_LABEL, MAX_DESCRIPTION = 1000, 80, 300
BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")

TOOL_SPEC = ToolSpec(
    name=NAME,
    description=(
        "Ask the user 1-4 multiple-choice questions and wait for the answers. Use it only for decisions that are the "
        "user's to make: a preference, a trade-off, an ambiguous requirement you cannot settle from the code or the "
        "conversation. Do not ask what you can find out yourself.\n"
        f"Each question has a short header (at most {MAX_HEADER} display cells, shown as a tab label), "
        f"{MIN_OPTIONS}-{MAX_OPTIONS} options with distinct labels and a one-line description each, and multiSelect "
        "when several options may be chosen together. Put the option you recommend first and end its label with "
        '"(Recommended)". The user can always type their own answer ("Type something") or choose to discuss '
        '("Chat about this"): those are added automatically, so never add Other/None options yourself.\n'
        "Never ask whether a plan is ok or whether to proceed: when in plan mode, ask for approval of the plan with "
        "exit_plan instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The full question, ending with '?'."},
                        "header": {
                            "type": "string",
                            "description": f"Tab label, at most {MAX_HEADER} display cells (a CJK character is two).",
                        },
                        "options": {
                            "type": "array",
                            "minItems": MIN_OPTIONS,
                            "maxItems": MAX_OPTIONS,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "The choice, 1-5 words."},
                                    "description": {"type": "string", "description": "What choosing it means."},
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multiSelect": {"type": "boolean", "description": "Allow choosing several options."},
                    },
                    "required": ["question", "header", "options", "multiSelect"],
                },
            }
        },
        "required": ["questions"],
    },
    side_effect=True,
)

UNATTENDED = (
    "Nobody can answer questions in this run: it is unattended (background, scheduled, loop, automation or goal "
    "continuation). Do not ask: pick the safest, most reversible option yourself, say which one you chose and why, "
    "and continue."
)
CHAT_INSTRUCTION = "Stop asking; discuss these questions with the user in conversation."


def _cells(text: str) -> int:
    """Display width in terminal cells: wide and fullwidth characters take two, combining marks none."""
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text
    )


def _field(where: str, name: str, value: Any, limit: int, *, newline: bool = False) -> str:
    """A model-supplied text field, stripped, or an error the model can act on (``ValueError``): control characters
    (ESC, BEL, NUL, TAB, DEL, C1 and the bidi controls) are refused, ``\n`` only where ``newline`` allows it."""
    text = str(value or "")
    for c in text:
        if (unicodedata.category(c) == "Cc" and not (newline and c == "\n")) or c in BIDI_CONTROLS:
            raise ValueError(f"{where}: {name} contains a control character (U+{ord(c):04X}); use plain text only")
    text = text.strip()
    if len(text) > limit:
        raise ValueError(f"{where}: {name} is {len(text)} characters; use at most {limit}")
    return text


def parse_questions(args: dict[str, Any]) -> list[dict[str, Any]] | str:
    """The model's questions in wire form (qid, question, header, choices, descriptions, multi_select), or an error the
    model can act on. ``tools.validate`` checks only the top level, so the nested shape is checked here."""
    raw = args.get("questions")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_QUESTIONS:
        return f"questions must be a list of 1-{MAX_QUESTIONS} questions"
    out: list[dict[str, Any]] = []
    for n, q in enumerate(raw, 1):
        where = f"question {n}"
        if not isinstance(q, dict):
            return f"{where} must be an object with question, header, options and multiSelect"
        try:
            text = _field(where, "question", q.get("question"), MAX_QUESTION, newline=True)
            if not text:
                return f"{where} has no question text"
            header = _field(where, "header", q.get("header"), 10**9)
            if not header:
                return f"{where} has no header (a tab label of at most {MAX_HEADER} display cells)"
            if (cells := _cells(header)) > MAX_HEADER:
                return f"{where}: header {header!r} is {cells} display cells wide; use at most {MAX_HEADER}"
        except ValueError as exc:
            return str(exc)
        multi = q.get("multiSelect", False)
        if not isinstance(multi, bool):
            return f"{where}: multiSelect must be true or false, not {multi!r}"
        options = q.get("options")
        if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            return (
                f"{where} needs {MIN_OPTIONS}-{MAX_OPTIONS} options (Type something and Chat about this are added "
                "automatically)"
            )
        labels: list[str] = []
        descriptions: list[str] = []
        for opt in options:
            try:
                label = _field(where, "option label", opt.get("label") if isinstance(opt, dict) else "", MAX_LABEL)
                if not label:
                    return f"{where}: every option needs a non-empty label"
                if label in labels:
                    return f"{where}: option label {label!r} appears twice; labels must be unique"
                labels.append(label)
                descriptions.append(
                    _field(where, "option description", opt.get("description"), MAX_DESCRIPTION, newline=True)
                )
            except ValueError as exc:
                return str(exc)
        out.append(
            {
                "qid": f"q{n}",
                "question": text,
                "header": header,
                "choices": labels,
                "descriptions": descriptions,
                "multi_select": multi,
            }
        )
    return out


def _selected(answer: str) -> list[str]:
    """A multi-select answer: the JSON array string of labels and typed text, or one typed string when it is not one."""
    try:
        parsed = json.loads(answer)
    except ValueError:
        return [answer]
    if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
        return [x for x in parsed if x.strip()]
    return [answer]


def decode_answers(questions: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    """The client's clarify result as the tool result the model sees.

    Only the qids sent with ``multi_select`` are decoded as JSON arrays: a typed answer to a single-select question can
    look like one (``["x"]``) and stays the string it is.
    """
    answers = result.get("answers")
    if not isinstance(answers, dict):
        lone = result.get("answer")  # a TUI build without the batch overlay answers the first question only
        answers = {"q1": lone} if isinstance(lone, str) and lone.strip() else {}
    entries: list[dict[str, Any]] = []
    unanswered: list[str] = []
    for q in questions:
        value = answers.get(q["qid"])
        if not isinstance(value, str) or not value.strip():
            unanswered.append(q["header"])
            continue
        entry: dict[str, Any] = {"header": q["header"], "question": q["question"]}
        if q["multi_select"]:
            picked = _selected(value)
            if not picked:
                unanswered.append(q["header"])
                continue
            entry["answers"] = picked
            entry["custom"] = any(p not in q["choices"] for p in picked)
        else:
            entry["answer"] = value
            entry["custom"] = value not in q["choices"]
        entries.append(entry)
    rest = {"unanswered": unanswered} if unanswered else {}
    if result.get("chat") is True:
        return {"user_chose_to_chat": True, "answers": entries, **rest, "instruction": CHAT_INSTRUCTION}
    if not entries:
        return {"error": "User dismissed the questions"}
    return {"answers": entries, **rest}


def register(tools: Any, server: Any, session: Any) -> None:
    """Add ``ask_user_question`` to a gateway loop's registry (``tools``), asking ``session``'s clients through
    ``server``. Registered on every loop, attended or not, so the tools array never changes mid-session; whether
    anyone can answer is decided per call."""

    async def handler(args: dict[str, Any], **_: Any) -> dict[str, Any]:
        from k3code.gateway.server import ApprovalTimeout  # the gateway imports this module

        if session.unattended:  # read per call: Ctrl+B, a goal or an automation can flip it mid-session
            return {"error": UNATTENDED}
        questions = parse_questions(args)
        if isinstance(questions, str):
            return {"error": questions}
        params = {"question": questions[0]["question"], "questions": questions}
        try:
            result = await server._ask_client("clarify", params, session.session_id)
        except ApprovalTimeout:
            minutes = int(server.approval_timeout_s // 60) or 1
            return {
                "error": f"The user did not answer within {minutes} min (the questions timed out). Do not ask again "
                "now: pick the safest, most reversible option, say which one you chose, and continue."
            }
        except RuntimeError as exc:  # the client refused the request (one that cannot show questions)
            return {"error": f"The questions could not be shown: {exc}. Continue without asking."}
        return decode_answers(questions, result if isinstance(result, dict) else {})

    tools.register(TOOL_SPEC, handler)
