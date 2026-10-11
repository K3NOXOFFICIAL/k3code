"""The curator's duplicate scan tokenises each skill once, not once per pair."""

from __future__ import annotations

from k3code.learning import curator


def skill(name: str, desc: str, text: str) -> dict:
    return {"name": name, "desc": desc, "text": text}


def test_near_duplicates_are_found_and_distinct_skills_are_not():
    a = skill("deploy-app", "deploy the app to staging", "run the build then push the image to the staging cluster")
    b = skill(
        "deploy-application", "deploy the app to staging", "run the build then push the image to the staging cluster"
    )
    c = skill("sql-migrate", "apply database migrations", "connect to postgres and apply pending schema changes")
    pairs = curator.duplicate_skills([a, b, c])
    assert [(x["name"], y["name"]) for x, y, _ in pairs] == [("deploy-app", "deploy-application")]
    assert pairs[0][2] >= curator.DUP_THRESHOLD
    assert curator.find_duplicates([a, c]) == [] and curator.duplicate_skills([]) == []


def test_each_skill_is_tokenised_once(monkeypatch):
    real = curator.tokens
    calls: list[str] = []

    def counting(text: str):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(curator, "tokens", counting)
    skills = [skill(f"skill-{i}", f"does thing {i}", f"steps for thing number {i} " * 20) for i in range(40)]
    curator.duplicate_skills(skills)
    assert len(calls) == 40  # one per skill; the pairwise version made 1560
