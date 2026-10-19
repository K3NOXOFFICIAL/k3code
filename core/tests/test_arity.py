"""Tests for bash arity prefix extraction."""

from __future__ import annotations

from k3code.permissions import arity


def test_unknown_single_token() -> None:
    assert arity.prefix(["mycmd"]) == ["mycmd"]


def test_unknown_returns_first_token() -> None:
    assert arity.prefix(["mycmd", "foo", "bar"]) == ["mycmd"]


def test_python_has_arity_two() -> None:
    assert arity.prefix(["python", "script.py"]) == ["python", "script.py"]


def test_empty() -> None:
    assert arity.prefix([]) == []


def test_explicit_arity_one() -> None:
    assert arity.prefix(["ls", "-la"]) == ["ls"]
    assert arity.prefix(["echo", "hello"]) == ["echo"]


def test_arity_two() -> None:
    assert arity.prefix(["git", "checkout", "main"]) == ["git", "checkout"]
    assert arity.prefix(["docker", "run", "nginx"]) == ["docker", "run"]


def test_arity_three_longest_wins() -> None:
    assert arity.prefix(["docker", "compose", "up", "-d"]) == ["docker", "compose", "up"]
    assert arity.prefix(["npm", "run", "dev"]) == ["npm", "run", "dev"]
    assert arity.prefix(["git", "config", "user.name"]) == ["git", "config", "user.name"]


def test_shorter_input_than_arity() -> None:
    assert arity.prefix(["git"]) == ["git"]
    assert arity.prefix(["npm", "run"]) == ["npm", "run"]
    assert arity.prefix(["aws", "s3"]) == ["aws", "s3"]


def test_systemctl() -> None:
    assert arity.prefix(["systemctl", "restart", "nginx"]) == ["systemctl", "restart"]


def test_prefix_only_joins_as_many_words_as_the_longest_key_has() -> None:
    from k3code.permissions import arity

    def old_prefix(tokens: list[str]) -> list[str]:  # the loop before the bound: every length from len(tokens) down
        for length in range(len(tokens), 0, -1):
            found = arity.ARITY.get(" ".join(tokens[:length]))
            if found is not None:
                return tokens[:found]
        return tokens[:1] if tokens else []

    samples = [
        [],
        ["git"],
        ["git", "commit", "-m", "x"],
        ["docker", "compose", "up", "-d"],
        ["python", "-m", "pytest", "-q"],
        ["terraform", "workspace", "list"],
        ["yarn", "dlx", "create-react-app", "app"],
        ["echo", "a", "b", "c"],
        ["vault", "kv", "get", "secret/x"],
    ]
    assert all(arity.prefix(s) == old_prefix(s) for s in samples)
    assert max(len(k.split()) for k in arity.ARITY) == arity._MAX_KEY_WORDS


def test_prefix_of_a_huge_command_takes_linear_time() -> None:
    import time

    from k3code.permissions import arity

    tokens = ["echo"] + ["a"] * 40_000
    t = time.perf_counter()
    arity.prefix(tokens)
    assert time.perf_counter() - t < 1.0  # was 3-5 s: one join of up to 40,000 words per length
