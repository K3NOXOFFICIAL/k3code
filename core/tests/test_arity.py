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
