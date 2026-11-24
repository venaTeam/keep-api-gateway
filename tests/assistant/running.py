"""Drives the real app: a scripted model, and requests whose stream can be watched and
left while it runs."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace

from src.services.assistant.core.work import OngoingWork
from src.services.assistant.llm.client import (
    Finished,
    TextDelta,
    ToolCallComplete,
    Usage,
)
from tests.assistant.fakes import TOKEN, FakeGateway, whoami


# --- the model -----------------------------------------------------------------


def says(*texts: str, reason: str = "stop") -> list:
    return [*(TextDelta(text) for text in texts), Usage(100, 10), Finished(reason)]


def calls(
    *arguments, text: str = "", name="change_alert_status", first: int = 1
) -> list:
    """One round that calls the tool once per entry of `arguments`."""
    return [
        *([TextDelta(text)] if text else []),
        *(
            ToolCallComplete(
                f"call_{first + n}", name, a if isinstance(a, str) else json.dumps(a)
            )
            for n, a in enumerate(arguments)
        ),
        Usage(100, 10),
        Finished("tool_calls"),
    ]


def change(*fingerprints: str, status="acknowledged", **values) -> dict:
    return {
        "fingerprints": list(fingerprints) or None,
        "cel": None,
        "status": status,
        "note": None,
        "dispose_on_new_alert": False,
        **values,
    }


class FakeLlm:
    """Each round is a list of events; an exception in it is raised, a function in it
    is awaited. The last round repeats."""

    model = "fake-model"

    def __init__(self, *rounds: list):
        self.rounds = list(rounds) or [says("Hello.")]
        self.requests: list[SimpleNamespace] = []
        self.open = 0
        self.closed = False

    async def stream(
        self, messages, tools, *, tool_choice="auto", safety_identifier=None
    ):
        self.requests.append(
            SimpleNamespace(
                messages=copy.deepcopy(messages),
                tools=tools,
                tool_choice=tool_choice,
                safety_identifier=safety_identifier,
            )
        )
        steps = self.rounds.pop(0) if len(self.rounds) > 1 else self.rounds[0]
        self.open += 1
        try:
            for step in steps:
                if isinstance(step, BaseException):
                    raise step
                if callable(step):
                    await step()
                else:
                    yield step
        finally:
            self.open -= 1

    async def aclose(self):
        self.closed = True


class Gate:
    """A step of a round that waits until it is opened."""

    def __init__(self):
        self.reached = asyncio.Event()
        self.opened = asyncio.Event()

    async def __call__(self):
        self.reached.set()
        await self.opened.wait()


# --- the app ---------------------------------------------------------------------


def equip(app, llm=None, gateway=None) -> OngoingWork:
    """What the lifespan gives a worker, with doubles for what is outside of it."""
    if gateway is None:
        gateway = FakeGateway().on_json("GET", "/whoami", whoami())
    app.state.assistant_work = OngoingWork()
    app.state.assistant_llm = llm
    app.state.assistant_gateway = (
        gateway.client() if isinstance(gateway, FakeGateway) else gateway
    )
    return app.state.assistant_work


class Exchange:
    """One request to the app, as the server makes it."""

    def __init__(
        self, app, method: str, path: str, body=None, *, token=TOKEN, on_send=None
    ):
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""
        # (seconds since the start, text) of every chunk
        self.chunks: list[tuple[float, str]] = []
        self._content = b"" if body is None else json.dumps(body).encode()
        self._sent_body = False
        self._left = asyncio.Event()
        self._news = asyncio.Event()
        self._on_send = on_send
        self._started = time.monotonic()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"gateway.test"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(self._content)).encode()),
                *([(b"authorization", token.encode())] if token else []),
            ],
            "client": ("192.0.2.1", 1234),
            "server": ("gateway.test", 80),
        }
        self.task = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self):
        if not self._sent_body:
            self._sent_body = True
            return {"type": "http.request", "body": self._content, "more_body": False}
        await self._left.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message):
        if self._on_send is not None:
            await self._on_send(message)
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {
                name.decode().lower(): value.decode()
                for name, value in message["headers"]
            }
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            self.body += chunk
            if chunk:
                self.chunks.append((time.monotonic() - self._started, chunk.decode()))
        self._news.set()

    def leave(self) -> None:
        """The client goes away."""
        self._left.set()

    async def finish(self, timeout: float = 20) -> "Exchange":
        await asyncio.wait_for(asyncio.shield(self.task), timeout)
        return self

    async def until(self, text: str, timeout: float = 10) -> None:
        """Until `text` has been received."""
        deadline = time.monotonic() + timeout
        while text not in self.text:
            assert time.monotonic() < deadline, f"{text!r} not in {self.text!r}"
            assert not self.task.done(), self.text
            self._news.clear()
            try:
                await asyncio.wait_for(self._news.wait(), 0.2)
            except TimeoutError:
                pass

    @property
    def text(self) -> str:
        return self.body.decode()

    def json(self):
        return json.loads(self.body)

    @property
    def code(self) -> str:
        return self.json()["errors"][0]["code"]

    @property
    def events(self) -> list[tuple[str, dict]]:
        found = []
        for frame in self.text.split("\n\n")[:-1]:
            lines = dict(
                line.split(": ", 1)
                for line in frame.split("\n")
                if not line.startswith(":")
            )
            if lines:
                found.append((lines["event"], json.loads(lines["data"])))
        return found

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def event(self, name: str) -> dict:
        """The last event of this name."""
        return [data for found, data in self.events if found == name][-1]


def post_chat(app, message="hello", conversation_id=None, on_send=None, **context):
    return Exchange(
        app,
        "POST",
        "/assistant/chat",
        {
            "conversation_id": conversation_id and str(conversation_id),
            "message": message,
            "context": context,
        },
        on_send=on_send,
    )


async def until(condition, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "did not happen"
        await asyncio.sleep(0.01)
