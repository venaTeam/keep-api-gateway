"""The libraries that could log a prompt or a token stay at WARNING or above."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NAMES = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
    "httpcore.http11",
)
REPORT = (
    "import logging\n"
    f"for name in {NAMES}:\n"
    "    print(name, logging.getLogger(name).getEffectiveLevel())\n"
    "print('root', logging.getLogger().level)\n"
)
CONFIGURE = "import src.utils.logging as l; l.setup_logging()\n"
CLIENT = "import src.services.assistant.llm.client\n"


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param(CONFIGURE + CLIENT, id="configured-then-imported"),
        pytest.param(CLIENT + CONFIGURE, id="imported-then-configured"),
        # Without the assistant: the gateway's own features import the SDK too.
        pytest.param("import openai\n" + CONFIGURE, id="gateway-only"),
        pytest.param(CLIENT + "logging.getLogger().setLevel(10)\n", id="client-only"),
    ],
)
def test_noisy_loggers_stay_pinned_with_debug_and_openai_log(steps):
    result = subprocess.run(
        [sys.executable, "-c", "import logging\n" + steps + REPORT],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "LOG_LEVEL": "DEBUG",
            "OPENAI_LOG": "debug",
            "KEEP_FLUENTBIT": "false",
            "KEEP_LOG_FILE": "",
            "DATABASE_CONNECTION_STRING": "sqlite://",
        },
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line]
    assert "root 10" in lines
    for name in NAMES:
        (level,) = [l.split()[1] for l in lines if l.split()[0] == name]
        assert int(level) >= 30, name
