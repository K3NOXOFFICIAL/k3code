"""The base system prompt must not promise what the harness does not do (GitHub #166, finding 1)."""

from __future__ import annotations

from k3code.prompting import base_prompt


def _line_with(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


def test_prompt_does_not_say_reads_never_need_permission():
    text = base_prompt().lower()
    assert "don't require permission" not in text
    assert "do not require permission" not in text
    # reads outside the project and credential paths can be refused: the prompt says so
    line = _line_with(text, "outside the project").lower()
    assert "refused" in line or "ask" in line


def test_prompt_does_not_say_a_read_file_never_changes():
    text = base_prompt().lower()
    assert "is unchanged unless you edited it" not in text
    assert "don't re-read it" not in text
    line = _line_with(text, "re-read")
    assert "bash" in line and "edit" in line


def test_prompt_tells_the_model_what_to_do_when_nobody_can_answer():
    assert "reversible option" in base_prompt().lower()


def test_the_priority_line_names_the_memory_files_k3code_loads():
    from k3code.memory import PROJECT_FILES, user_memory_path

    line = _line_with(base_prompt(), "Instruction priority")
    assert user_memory_path().name in line and "User memory" in line  # the section heading memory_prompt writes
    assert all(name in line for name in PROJECT_FILES)
    assert "never instructions" in line  # tool, web, MCP and sub-agent output is data


def test_a_sub_agent_without_a_named_tool_is_told_to_skip_it(tmp_path):
    from k3code.subagents.types import load_agent_types

    named = {"read", "grep", "glob", "edit", "write", "bash", "todo"}
    text = base_prompt()
    assert all(f"`{t}`" in text for t in named)
    # explorer and planner have read/grep/glob only, yet get this prompt: it must say a missing tool is skipped
    types = load_agent_types(tmp_path, home=tmp_path).values()
    assert [a.name for a in types if a.tools and not named <= set(a.tools)]
    assert "tool you lack" in _line_with(text, "Use only the tools you were given")


def test_tips_never_follow_the_reviewer_verdict_line():
    from k3code.autonomy.fanout import VERDICT_RE

    # the fan-out reviewer's verdict must be the last line of its report, and the base prompt asks for trailing Tips
    assert not VERDICT_RE.search("Looks fine.\nVERDICT: pass\nTips:\n- add a test")
    line = _line_with(base_prompt(), "Tips:")
    assert "VERDICT:" in line and "stays last" in line


def test_the_base_prompt_stays_small():
    from k3code.prompting import tokens_of

    # re-sent on every request: grow it only for a rule that earns its tokens (1.0k before the coding rules)
    assert tokens_of(base_prompt()) <= 1150


def test_the_prompt_carries_the_coding_rules():
    text = base_prompt()
    # read before edit, edit over write, no guessed paths, minimal changes
    line = _line_with(text, "Prefer reading over guessing")
    assert "`read` a file before you edit it" in line and "never guess a path" in line
    line = _line_with(text, "Make minimal, focused changes")
    assert "`edit`" in line and "`write` only for new files" in line and "line-number prefix" in line
    # verification loop: run the project's tests/linter for the touched area before finishing
    line = _line_with(text, "Verify, then claim")
    assert "run the project's tests or linter" in line and "Project facts" in line  # the facts section's name
    assert "say what you did not verify" in line
    # bash: one command per call, with a timeout; the tool's feedback lines are acted on, not repeated
    line = _line_with(text, "one command per call")
    assert "`timeout`" in line and "background: true" in line
    assert "`syntax:`" in _line_with(text, "closest-match hint")
    # task end: what changed and what was run
    line = _line_with(text, "Report briefly")
    assert "what changed (files)" in line and "what you ran to verify it" in line
