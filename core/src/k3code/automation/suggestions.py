# Dedup-latch design ported from hermes-agent cron/suggestions.py and cron/suggestion_catalog.py (MIT).
"""Starter automations offered to the user. A dismissed or accepted suggestion latches its dedup key forever."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from k3code.automation.store import AutomationDB, new_id

MAX_PENDING = 5


@dataclass(frozen=True)
class CatalogEntry:
    key: str  # stable dedup key
    title: str
    description: str
    spec: dict[str, Any]  # {name, trigger, action, policy?}


CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        "catalog:nightly-tests",
        "Nightly test run",
        "Every night at 02:00, run the project's test suite and report failures.",
        {
            "name": "nightly tests",
            "trigger": {"type": "cron", "schedule": "0 2 * * *"},
            "action": {
                "type": "prompt",
                "prompt": "Run this project's test suite. Report which tests fail and the most "
                "likely cause of each. Do not change any files.",
            },
        },
    ),
    CatalogEntry(
        "catalog:morning-git-summary",
        "Morning git summary",
        "Weekdays at 08:30, summarise what changed in the repo since yesterday.",
        {
            "name": "morning git summary",
            "trigger": {"type": "cron", "schedule": "30 8 * * 1-5"},
            "action": {
                "type": "prompt",
                "prompt": "Summarise the git activity in this repository since yesterday "
                "(commits, authors, notable files). Keep it under 10 lines.",
            },
        },
    ),
    CatalogEntry(
        "catalog:dependency-update-check",
        "Dependency update check",
        "Mondays at 09:00, check for outdated dependencies and report the risky ones.",
        {
            "name": "dependency update check",
            "trigger": {"type": "cron", "schedule": "0 9 * * 1"},
            "action": {
                "type": "prompt",
                "prompt": "Check this project's dependencies for newer versions (read-only: do "
                "not install or edit anything). List the outdated ones and flag breaking or security-relevant updates.",
            },
        },
    ),
    CatalogEntry(
        "catalog:notify-needs-input",
        "Notify when a session needs input",
        "Get a notification whenever a background session stops and waits for you.",
        {
            "name": "notify on needs_input",
            "trigger": {"type": "session_event", "event": "needs_input"},
            "action": {"type": "notify", "text": "A session needs your input ({{session}})."},
            "policy": {"cooldown_s": 60},
        },
    ),
]


class Suggestions:
    def __init__(self, db: AutomationDB, catalog: list[CatalogEntry] | None = None) -> None:
        self.db = db
        self.catalog = CATALOG if catalog is None else catalog

    def add(
        self, *, title: str, description: str, source: str, spec: dict[str, Any], dedup_key: str
    ) -> dict[str, Any] | None:
        """Register a pending suggestion; None when the key was already offered (any status) or the backlog is full."""
        if not title.strip() or not dedup_key.strip():
            raise ValueError("title and dedup_key are required")
        if self.db.rows("suggestions", "dedup_key=?", (dedup_key,)):
            return None
        if len(self.pending()) >= MAX_PENDING:
            return None
        sid = new_id()
        self.db.insert(
            "suggestions",
            id=sid,
            dedup_key=dedup_key,
            title=title,
            description=description,
            source=source,
            spec=spec,
            created_at=time.time(),
        )
        return self.db.get("suggestions", sid)

    def pending(self) -> list[dict[str, Any]]:
        return self.db.rows("suggestions", "status='pending'", order="created_at, rowid")

    def suggest(self) -> list[dict[str, Any]]:
        """Seed the catalog (entries never offered before) and return everything not yet dismissed or accepted."""
        for e in self.catalog:
            self.add(title=e.title, description=e.description, source="catalog", spec=e.spec, dedup_key=e.key)
        return self.pending()

    def get(self, ref: str) -> dict[str, Any] | None:
        row = self.db.find("suggestions", ref)
        if row is not None:
            return row
        pending = self.pending()
        if ref.isdigit() and 1 <= int(ref) <= len(pending):
            return pending[int(ref) - 1]
        return next((s for s in self.db.rows("suggestions") if s["title"].lower() == ref.lower()), None)

    def dismiss(self, ref: str) -> bool:
        s = self.get(ref)
        if s is None or s["status"] != "pending":
            return False
        self.db.update("suggestions", s["id"], status="dismissed", resolved_at=time.time())
        return True

    def mark_accepted(self, ref: str) -> dict[str, Any] | None:
        """Return the spec to turn into an automation and latch the key; None if unknown or already resolved."""
        s = self.get(ref)
        if s is None or s["status"] != "pending":
            return None
        self.db.update("suggestions", s["id"], status="accepted", resolved_at=time.time())
        return s
