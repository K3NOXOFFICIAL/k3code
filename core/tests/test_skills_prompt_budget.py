"""The skills section stays small however many skills are installed: names are capped, the rest are found by query."""

from k3code.prompting import tokens_of
from k3code.skills import PROMPT_LIMIT, search, skills_prompt

N_SKILLS = 85
#: 30 lines of name + a 90-char description is about 860 tokens; listing all 85 in full would be about 9k.
BUDGET_TOKENS = 1200


def _install(root, n):
    for i in range(n):
        d = root / f"skill-{i:03d}"
        d.mkdir(parents=True)
        desc = f"Use for topic{i} work. " + "Long description text that goes on and on. " * 8
        (d / "SKILL.md").write_text(f"---\nname: skill-{i:03d}\ndescription: {desc}\n---\nBody {i}\n")


def test_skills_section_stays_under_budget_with_many_skills(tmp_path):
    roots = tmp_path / "skills"
    _install(roots, N_SKILLS)

    prompt = skills_prompt(str(tmp_path), [str(roots)])

    listed = [line for line in prompt.splitlines() if line.startswith("- skill-")]
    assert len(listed) == PROMPT_LIMIT
    assert f"…and {N_SKILLS - PROMPT_LIMIT} more" in prompt
    assert tokens_of(prompt) <= BUDGET_TOKENS


def test_an_unlisted_skill_is_found_by_query(tmp_path):
    roots = tmp_path / "skills"
    _install(roots, N_SKILLS)
    prompt = skills_prompt(str(tmp_path), [str(roots)])
    unlisted = next(f"skill-{i:03d}" for i in range(N_SKILLS) if f"- skill-{i:03d}:" not in prompt)

    hits = search(f"topic{int(unlisted[-3:])}", str(tmp_path), [str(roots)], limit=1)

    assert [s.name for s in hits] == [unlisted]
