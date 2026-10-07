# PROGRESS (exit-verify)
Done: scripts/exit lib/runner/render; m0,m2,m3,m4,m5,m6 checks; soak; tui_harness; m1_tui.py (all flows except live review untested); sync-upstream.sh; /help dedupe.
Gateway fixes found by M1: session.close RPC, needs_input state while approval open, resume/activate transcript rows + deferred approval replay.
Next: run `scripts/exit/run_all.sh` (30 min soak), fix FAILs, write REPORT.md.
