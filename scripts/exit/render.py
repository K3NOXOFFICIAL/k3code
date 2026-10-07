"""Render JSONL rows into docs/reports/exit-status.md."""
import json
import sys
import time
from collections import Counter
from pathlib import Path

rows = [json.loads(ln) for ln in Path(sys.argv[1]).read_text().splitlines() if ln.strip()]
order = {m: i for i, m in enumerate(["M0", "M1", "M2", "M3", "M4", "M5", "M6"])}
rows.sort(key=lambda r: order.get(r["milestone"], 99))
c = Counter(r["status"] for r in rows)


def cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", "<br>")


out = ["# Exit-criteria status", "",
       f"Generated {time.strftime('%Y-%m-%d %H:%M:%S %z')} by `scripts/exit/run_all.sh`.",
       f"**{c['PASS']} PASS, {c['FAIL']} FAIL, {c['PENDING']} PENDING** ({len(rows)} rows).", "",
       "| Milestone | Criterion | How checked | Status | Evidence (output tail) |", "|---|---|---|---|---|"]
for r in rows:
    ev = r["evidence"] + (f"\n**To close:** {r['close']}" if r["status"] == "PENDING" else "")
    out.append(f"| {r['milestone']} | {cell(r['criterion'])} | {cell(r['how'])} | **{r['status']}** | {cell(ev)} |")
Path(sys.argv[2]).write_text("\n".join(out) + "\n")
