"""Proposal ids are unique across processes that share ``proposals.jsonl`` (daemon, standalone gateway, CLI)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

from k3code.autonomy.proposals import ProposalStore

_WORKER = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from k3code.autonomy.proposals import ProposalStore

    home, tag, n, go = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
    while not go.exists():
        time.sleep(0.001)
    store = ProposalStore(home)
    made = [store.add("improvement", f"{tag} proposal {i}", "do", key=f"{tag}-{i}") for i in range(n)]
    print(json.dumps([[p.id, p.text] for p in made if p is not None]))
    """
)


def _run_workers(home, procs: int, n: int, tmp_path):
    go = tmp_path / "go"
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(home), f"w{i}", str(n), str(go)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for i in range(procs)
    ]
    go.write_text("go")
    made: list[list] = []
    for c in children:
        out, _ = c.communicate(timeout=120)
        assert c.returncode == 0
        made += json.loads(out)
    return made


def test_four_processes_adding_at_once_never_share_an_id(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    made = _run_workers(home, 4, 50, tmp_path)
    assert len(made) == 200
    ids = [m[0] for m in made]
    assert len(set(ids)) == 200, "two proposals got the same id"
    store = ProposalStore(home)
    assert len(store.all()) == 200
    for pid, text in made:  # accepting a card must find the proposal that card was made from
        assert store.get(pid).text == text


def test_existing_p_numbered_ids_keep_counting(tmp_path):
    store = ProposalStore(tmp_path)
    assert [store.add("improvement", f"t{i}", "a").id for i in range(3)] == ["p1", "p2", "p3"]
    assert ProposalStore(tmp_path).add("improvement", "t9", "a").id == "p4"


def test_the_duplicate_key_check_sees_what_another_store_instance_added(tmp_path):
    a, b = ProposalStore(tmp_path), ProposalStore(tmp_path)
    assert a.add("improvement", "same", "x") is not None
    assert b.add("improvement", "same", "x") is None  # b's cached view is stale: it must look again
    assert b.add("improvement", "other", "x").id == "p2"
    assert a.get("p2").text == "other"


def test_set_status_does_not_change_what_a_caller_holds(tmp_path):
    store = ProposalStore(tmp_path)
    p = store.add("improvement", "t", "a")
    held = store.get(p.id)
    store.set_status(p.id, "dismissed")
    assert held.status == "pending" and store.get(p.id).status == "dismissed"
    assert store.all()[0].status == "dismissed"
