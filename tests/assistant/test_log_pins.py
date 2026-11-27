"""What the assistant's own lines carry, and the libraries that could log a prompt or a
token stay at WARNING or above."""
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.services.assistant.bl import confirm_bl
from src.services.assistant.kinds import alert_status
from src.services.assistant.models.api.draft import DraftObject
from tests.assistant import golden_stream
from tests.assistant.fakes import identity
from tests.assistant.logs import rendered

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


CANARY = "canary-object-name-7c1d"


@pytest.mark.parametrize("with_object", [True, False], ids=["object", "no-object"])
def test_a_confirm_logs_what_it_wrote_by_id_never_by_name(caplog, with_object):
    draft = golden_stream.drafts()["applied"]
    outcome = draft.outcome.copy()
    if with_object:
        outcome.object = DraftObject(
            type="preset", id=golden_stream.uid(40), name=CANARY, path=f"/{CANARY}"
        )
    attempt = confirm_bl._Attempt(
        identity(), draft, None, None, time.monotonic(), kind=alert_status.KIND
    )
    with caplog.at_level(logging.DEBUG):
        attempt._report(draft, ("applied", outcome))
    [line] = [r for r in caplog.records if r.message == "assistant: draft confirmed"]
    expected = ("preset", golden_stream.uid(40)) if with_object else (None, None)
    assert (line.object_type, line.object_id) == expected
    assert (line.outcome, line.draft_id) == ("applied", str(draft.id))
    assert CANARY not in rendered(line)
