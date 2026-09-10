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
            return object()

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
async def test_runner_uses_compact_structured_output_options() -> None:
    class Agent:
        seen_options: dict[str, object] | None = None

        async def run(self, _: object, *, options: dict[str, object]) -> object:
            self.seen_options = options
            return object()

    agent = Agent()
    runner = AgentFrameworkJsonRunner("test-key", "test-model", max_output_tokens=384)

    assert await runner._run_with_backoff(agent, "prompt", _ResponseModel)
    assert agent.seen_options == {
        "response_format": _ResponseModel,
        "max_tokens": 384,
        "verbosity": "low",
    }
