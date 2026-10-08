"""`k3code -p` printed httpx request lines and "Turn N/20" (INFO) between the words of its answer."""

import os
import subprocess
import sys


def _root_level(env_extra: dict[str, str]) -> str:
    env = {k: v for k, v in os.environ.items() if k != "K3CODE_LOG_LEVEL"} | env_extra
    code = "import logging, k3code.cli; print(logging.getLevelName(logging.getLogger().level))"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout


def test_the_cli_logs_warnings_only_by_default():
    assert _root_level({}).strip() == "WARNING"


def test_k3code_log_level_still_turns_info_on():
    assert _root_level({"K3CODE_LOG_LEVEL": "info"}).strip() == "INFO"
