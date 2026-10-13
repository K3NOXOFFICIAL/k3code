"""Sizing: plan steps -> independent units -> how many workers fit the budget (pure functions, no model)."""

from __future__ import annotations

import pytest

from k3code.autonomy.sizing import (
    MAX_SUBTASKS,
    REVIEW_ATTEMPTS,
    REVIEW_HOLD,
    REVIEW_LENSES,
    WORKER_AGENTS,
    group_steps,
    pack,
    pick_planners,
    size_agents,
    top_level_steps,
    unit_text,
)
from k3code.autonomy.ultra import LENSES


def plan(steps: str) -> str:
    return f"## Goal\nx\n## Steps\n{steps}\n## Files\nnone\n## Risks\nnone\n## Verification\nrun\n## Estimate\nlarge"


def units(steps: str) -> list[str]:
    return [unit_text(u) for u in group_steps(top_level_steps(plan(steps)))]


def test_only_top_level_steps_count_and_parallel_marks_are_stripped():
    text = (
        "1. add the parser (parallel)\n   - handle commas\n   - handle quotes\n"
        "2. wire it up\n   - call it from main\n3. docs"
    )
    steps = top_level_steps(plan(text))
    assert [s.title for s in steps] == ["add the parser", "wire it up", "docs"]
    assert [s.parallel for s in steps] == [True, False, False]


def test_bullets_and_numbers_both_count_and_a_plan_without_steps_has_none():
    assert [s.title for s in top_level_steps(plan("- one\n- two"))] == ["one", "two"]
    assert top_level_steps("just prose") == []
    assert top_level_steps(plan("")) == []


def test_unmarked_steps_that_touch_different_files_stay_separate():
    assert units("1. edit core/a.py\n2. edit core/b.py\n3. update docs/guide.md") == [
        "edit core/a.py",
        "edit core/b.py",
        "update docs/guide.md",
    ]


def test_steps_that_name_the_same_file_or_say_after_step_n_join():
    assert units("1. change core/a.py\n2. test core/a.py\n3. edit core/c.py") == [
        "change core/a.py; then test core/a.py (in this order)",
        "edit core/c.py",
    ]
    assert units("1. create the schema\n2. migrate rows after step 1\n3. update docs") == [
        "create the schema; then migrate rows after step 1 (in this order)",
        "update docs",
    ]


def test_with_marks_each_marked_step_is_a_unit_and_the_rest_is_one_chain():
    got = units("1. build A (parallel)\n2. build B (parallel)\n3. glue A and B\n4. run the checks")
    assert got == ["build A", "build B", "glue A and B; then run the checks (in this order)"]


def test_a_marked_step_that_waits_for_an_unmarked_one_joins_its_chain():
    got = units("1. setup\n2. build A (parallel) after step 1\n3. build B (parallel)")
    assert got == ["setup; then build A after step 1 (in this order)", "build B"]


def test_pack_joins_contiguous_units_and_drops_none():
    assert pack(["a", "b", "c"], 3) == ["a", "b", "c"]
    assert pack(["a", "b", "c", "d", "e"], 3) == [
        "a; then b (in this order)",
        "c; then d (in this order)",
        "e",
    ]
    assert pack(["a"], 3) == ["a"]
    assert pack(["a", "b"], 0) == []


def verdict(scope="small", *, risk="low", needs_plan=False, source="classifier"):
    from types import SimpleNamespace

    return SimpleNamespace(scope=scope, risk=risk, needs_plan=needs_plan, source=source)


ALL = ["mvp-first", "risk-first", "architecture-first"]


@pytest.mark.parametrize(
    ("given", "angles"),
    [
        (verdict("small"), ["mvp-first"]),
        (verdict("trivial"), ["mvp-first"]),
        (verdict("medium"), ALL),
        (verdict("large"), ALL),
        (verdict("small", risk="high"), ALL),  # the safety floor found a danger class
        (verdict("small", needs_plan=True), ALL),
        (verdict("small", source="override"), ALL),  # /scope small: the user said so, nobody measured
        (verdict("small", source="fallback"), ALL),  # the classifier was down
        (None, ALL),  # /ultracode <task>, the wake word
    ],
)
def test_one_planner_only_for_a_classifier_rated_small_low_risk_task(given, angles):
    assert pick_planners(given) == angles


def sized(n_units: int, left: int | None, **kw):
    return size_agents(
        [f"u{i}" for i in range(n_units)], whole="WHOLE", left=left, width_cap=kw.pop("width_cap", 3), **kw
    )


def test_the_review_tail_pays_for_every_lens_asked_again_and_the_fixer():
    # a lens that answers without its findings is asked once more: that must never take the fixer's agent
    assert len(LENSES) == REVIEW_LENSES
    assert REVIEW_HOLD == len(LENSES) * REVIEW_ATTEMPTS + 1 == 5


def test_a_worker_is_sized_for_its_rework_and_both_reviews_each_asked_again():
    # worker + rework, and the review of the result and the re-review of the rework, each asked REVIEW_ATTEMPTS times
    assert WORKER_AGENTS == 2 + 2 * REVIEW_ATTEMPTS == 6


def test_the_default_budget_pays_for_two_workers_and_keeps_the_review_tail():
    for n in (3, 4, 5, 8):
        got = sized(n, 9)  # 12 agents minus 3 planners
        assert got.workers == 2 and got.hold == REVIEW_HOLD
        assert got.workers * 2 + got.hold <= 9
    assert sized(4, 9).subtasks == ["u0; then u1 (in this order)", "u2; then u3 (in this order)"]
    assert "4 units packed into 2 worker(s) by the agent budget" in sized(4, 9).reasons
    assert sized(3, 11).workers == 3  # three workers need 14 agents: 3 planners, 3 workers, 3 reviewers, the tail


def test_a_bigger_budget_buys_more_workers_up_to_the_scope_and_the_limit():
    assert sized(8, 13).workers == 4  # (13 - 5 held) // 2
    assert sized(8, 17).workers == 6  # (17 - 5 held) // 2: no scope caps it below the budget
    assert sized(8, 17, scope="medium").workers == 6
    assert sized(8, 60, scope="large").workers == 8  # the limit of eight, whatever the budget
    assert sized(8, 60, scope="huge").workers == 8
    assert sized(2, 60).workers == 2  # never more than the plan has units


def test_one_unit_or_a_small_scope_runs_the_whole_task_as_one_worker():
    for got in (sized(1, 9), sized(5, 9, scope="small"), sized(0, 9)):
        assert got.workers == 1 and got.subtasks == ["WHOLE"]


def test_a_tight_budget_cuts_the_tail_before_it_cuts_the_workers():
    got = sized(4, 4)
    assert got.workers == 1 and got.hold == 2  # (4 - 5) // 2 would be no worker: the tail shrinks to 2
    assert any("review tail cut" in r for r in got.reasons)
    tiny = sized(4, 1)
    assert tiny.workers == 0 and "raise it with /maxagents" in tiny.reasons[0]


def test_width_is_bounded_by_workers_and_the_cap():
    assert sized(3, 9, width_cap=2).width == 2
    assert sized(3, 11, width_cap=8).width == 3
    assert sized(1, 9, width_cap=4).width == 1


def test_a_nearly_spent_token_budget_limits_the_workers_but_never_below_one():
    assert sized(6, 40, tokens_left=600_000).workers == 2  # 600k // (2 * 150k)
    assert sized(6, 40, tokens_left=0).workers == 1
    assert sized(6, None).workers == 6  # no agent limit at all


def test_a_property_the_workers_never_outspend_the_budget_they_were_sized_from():
    for left in range(2, 40):
        for n in range(0, 10):
            got = sized(n, left)
            if got.workers:
                assert got.workers * 2 + got.hold <= max(left, 2)
                assert got.workers <= max(1, min(n, MAX_SUBTASKS))


# ── what the parser takes from a plan ──


def test_the_files_marker_names_a_steps_files_and_is_not_part_of_its_title():
    steps = top_level_steps(
        plan("1. add the parser [files: core/parse.py, core/lex.py] (parallel)\n2. write docs [files: docs/]")
    )
    assert [s.title for s in steps] == ["add the parser", "write docs"]
    assert steps[0].paths == {"core/parse.py", "core/lex.py"} and steps[1].paths == {"docs"}
    assert steps[0].parallel and not steps[1].parallel


def test_two_steps_that_name_the_same_file_stay_together_even_when_both_are_marked_parallel():
    got = units(
        "1. add a flag [files: cli.py] (parallel)\n2. document the flag [files: cli.py] (parallel)\n3. add tests"
    )
    assert got == ["add a flag; then document the flag (in this order)", "add tests"]


def test_marked_steps_that_overlap_are_one_unit():
    grouped = group_steps(
        top_level_steps(
            plan("1. a [files: cli.py] (parallel)\n2. b [files: cli.py] (parallel)\n3. c [files: other.py]")
        )
    )
    assert [[s.title for s in u] for u in grouped] == [["a", "b"], ["c"]]


@pytest.mark.parametrize(
    "text",
    [
        "1. read/write the config and node.js bits\n2. open and/or close the file",  # prose with slashes and a product
    ],
)
def test_prose_that_looks_like_a_path_does_not_join_steps(text):
    assert len(group_steps(top_level_steps(plan(text)))) == 2


def test_a_known_extension_or_a_directory_is_a_path_and_nothing_else_is():
    steps = top_level_steps(plan("1. edit src/app/main.py and README.md\n2. clear build/ output\n3. see read/write"))
    assert steps[0].paths == {"src/app/main.py", "readme.md"}
    assert steps[1].paths == {"build"} and steps[2].paths == frozenset()


def test_numbered_items_are_the_steps_when_bullets_share_their_indent():
    steps = top_level_steps(plan("1. build the thing\n- note: keep it small\n2. test the thing\n- note: use pytest"))
    assert [s.title for s in steps] == ["build the thing", "test the thing"]


def test_a_code_fence_inside_steps_is_not_steps():
    text = "1. run the generator\n```\n- not a step\n- nor this\n```\n2. check the output"
    assert [s.title for s in top_level_steps(plan(text))] == ["run the generator", "check the output"]


def test_a_step_can_wait_for_several_steps():
    grouped = group_steps(top_level_steps(plan("1. a\n2. b\n3. c\n4. d after steps 1 and 3")))
    assert [[s.title for s in u] for u in grouped] == [["a", "c", "d after steps 1 and 3"], ["b"]]
