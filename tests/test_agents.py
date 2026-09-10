from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from vtuber_dictionary.agents import AgentFrameworkJsonRunner


class _ResponseModel(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_runner_retries_a_rate_limit_wrapped_by_agent_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimited(Exception):
        status_code = 429

        class response:
            headers = {"retry-after-ms": "1"}

    class AgentFrameworkWrapper(Exception):
        pass

    class Agent:
        calls = 0

        async def run(self, *_: object, **__: object) -> object:
            self.calls += 1
            if self.calls == 1:
                raise AgentFrameworkWrapper("wrapped", RateLimited())
            return type("Valid", (), {"text": '{"value":"ok"}'})()

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    agent = Agent()

    runner = AgentFrameworkJsonRunner("test-key", "test-model")

    assert await runner._run_with_backoff(agent, "prompt", _ResponseModel)
    assert agent.calls == 2
    assert any(delay >= 1 for delay in delays)


@pytest.mark.asyncio
async def test_runner_retries_a_wrapped_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class APIConnectionError(Exception):
        pass

    class Agent:
        calls = 0

        async def run(self, *_: object, **__: object) -> object:
            self.calls += 1
            if self.calls == 1:
                raise Exception("wrapped", APIConnectionError())
            return type("Valid", (), {"text": '{"value":"ok"}'})()

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    agent = Agent()
    runner = AgentFrameworkJsonRunner("test-key", "test-model")
    assert await runner._run_with_backoff(agent, "prompt", _ResponseModel)
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_runner_retries_a_wrapped_bad_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    class BadGateway(Exception):
        status_code = 502

    class Agent:
        calls = 0

        async def run(self, *_: object, **__: object) -> object:
            self.calls += 1
            if self.calls == 1:
                raise Exception("wrapped", BadGateway())
            return type("Valid", (), {"text": '{"value":"ok"}'})()

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    agent = Agent()
    runner = AgentFrameworkJsonRunner("test-key", "test-model")
    assert await runner._run_with_backoff(agent, "prompt", _ResponseModel)
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_runner_uses_structured_output_options() -> None:
    class Agent:
        seen_options: dict[str, object] | None = None

        async def run(self, _: object, *, options: dict[str, object]) -> object:
            self.seen_options = options
            return type("Valid", (), {"text": '{"value":"ok"}'})()

    agent = Agent()
    runner = AgentFrameworkJsonRunner("test-key", "test-model")

    assert await runner._run_with_backoff(agent, "prompt", _ResponseModel)
    assert agent.seen_options == {
        "response_format": _ResponseModel,
    }


@pytest.mark.asyncio
async def test_runner_retries_an_empty_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Agent:
        calls = 0

        async def run(self, *_: object, **__: object) -> object:
            self.calls += 1
            if self.calls == 1:
                return type("Empty", (), {"text": ""})()
            return type("Valid", (), {"text": '{"value":"ok"}'})()

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    agent = Agent()
    runner = AgentFrameworkJsonRunner("test-key", "test-model")

    assert await runner._run_with_backoff(agent, "prompt", _ResponseModel) == '{"value":"ok"}'
    assert agent.calls == 2
