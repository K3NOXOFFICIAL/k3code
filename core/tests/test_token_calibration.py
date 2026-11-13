"""The token estimate (bytes/4) is corrected by what the provider reports, and elision leaves room for the reply."""

from __future__ import annotations

from k3code import context_budget
from k3code.context_budget import CALIBRATION_MAX, CALIBRATION_MIN, TokenCalibration, elide_threshold
from k3code.providers.types import Message, StreamEvent, Usage
from test_token_context_budget import make_loop, reads_of, run_reads
from test_wire_clip import Recorder, text_reply, tool_reply

# --- the calibration


def test_without_a_report_the_estimate_is_unchanged() -> None:
    cal = TokenCalibration()
    assert cal.ratio == 1.0 and cal.correct(1234) == 1234


def test_the_first_report_sets_the_ratio() -> None:
    cal = TokenCalibration()
    cal.observe(1000, 800)  # the provider counted 1000 where the estimate said 800
    assert cal.ratio == 1.25
    assert abs(cal.correct(800) - 1000) <= 1000 * 0.05  # within 5% of what the provider counted


def test_later_reports_move_it_gradually() -> None:
    cal = TokenCalibration()
    cal.observe(1000, 800)  # 1.25
    cal.observe(800, 800)  # 1.0
    assert 1.0 < cal.ratio < 1.25
    for _ in range(40):
        cal.observe(800, 800)
    assert abs(cal.ratio - 1.0) < 0.01


def test_a_wild_report_is_clamped() -> None:
    cal = TokenCalibration()
    cal.observe(10_000_000, 100)
    assert cal.ratio == CALIBRATION_MAX
    fresh = TokenCalibration()
    fresh.observe(1, 1_000_000)
    assert fresh.ratio == CALIBRATION_MIN


def test_a_report_without_numbers_is_ignored() -> None:
    cal = TokenCalibration()
    for real, est in ((0, 800), (-5, 800), (1000, 0), (1000, -1)):
        cal.observe(real, est)
    assert cal.ratio == 1.0 and cal.samples == 0


# --- the elision threshold leaves room for the reply


def test_a_big_window_keeps_the_plain_share() -> None:
    assert elide_threshold(200_000, 0.5, 8192) == 100_000


def test_a_small_window_elides_earlier_so_the_reply_fits() -> None:
    got = elide_threshold(16_000, 0.5, 8192)
    assert got < 16_000 * 0.5 and got == 16_000 - 8192 - context_budget.RESERVE_MARGIN


def test_a_window_the_reply_alone_would_fill_keeps_the_plain_share() -> None:
    assert elide_threshold(8_000, 0.5, 8192) == 4_000


# --- in the loop


async def test_the_loop_learns_the_ratio_from_the_providers_usage(tmp_path) -> None:
    loop = make_loop(tmp_path, Recorder([text_reply("ok")]), window=100_000)
    loop._last_estimate = 800
    done = Message(role="assistant", content="x", usage=Usage(prompt_tokens=1000))
    loop._record_usage(StreamEvent(type="done", message=done))
    assert loop.calibration.ratio == 1.25


async def test_no_estimate_yet_means_no_learning(tmp_path) -> None:
    loop = make_loop(tmp_path, Recorder([text_reply("ok")]), window=100_000)
    done = Message(role="assistant", content="x", usage=Usage(prompt_tokens=1000))
    loop._record_usage(StreamEvent(type="done", message=done))
    assert loop.calibration.samples == 0


async def test_the_estimate_paired_with_the_providers_count_counts_the_system_prompt_once(tmp_path) -> None:
    """The system prompt is part of the request twice over in the loop's list (as a message and in the overhead): the
    ratio is learned on one count and applied by the gateway to one count (`_request_tokens`)."""
    loop = make_loop(tmp_path, Recorder([text_reply("ok")]), window=1_000_000)
    loop.system_prompt = "S" * 40_000  # about 10k tokens
    async for _ in loop.run("hello"):
        pass
    overhead = context_budget.overhead_tokens(loop.system_prompt, loop.tool_specs())
    once = overhead + context_budget.message_tokens([Message(role="user", content="hello")])
    assert abs(loop._last_estimate - once) <= once * 0.01
    assert loop._last_estimate < overhead + 10_000 * 1.5  # not another 10k for the same prompt


def _any_elided(provider: Recorder) -> bool:
    return any((m.content or "").startswith("[earlier") for m in provider.requests[-1][0] if m.role == "tool")


async def test_a_ratio_above_one_makes_elision_start_earlier(tmp_path) -> None:
    unbounded, _ = await run_reads(tmp_path, window=1_000_000)  # nothing is elided: the full size of the request
    window = int(unbounded._last_estimate * 2.6)  # the raw estimate stays under half of it...

    _, provider = await run_reads(tmp_path, window=window)
    assert not _any_elided(provider)

    provider = Recorder([*(tool_reply(c) for c in reads_of(tmp_path, 9)), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=window)
    loop.calibration.observe(2000, 1000)  # ...but the provider counts twice what the estimate says
    async for _ in loop.run("read them all"):
        pass
    assert _any_elided(provider)
