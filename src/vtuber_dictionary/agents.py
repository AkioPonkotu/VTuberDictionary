"""Independent Microsoft Agent Framework-backed research and verification agents."""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel

from .domain import AudienceMetrics, Candidate, ResearchResult, VerificationResult


class JsonAgentRunner(Protocol):
    async def run_json(
        self, instructions: str, prompt: str, response_model: type[BaseModel]
    ) -> str: ...


class AgentFrameworkJsonRunner:
    """Adapter around Microsoft Agent Framework's OpenAI chat client.

    ``agent-framework-openai`` is intentionally imported only when a research
    stage runs, keeping discovery-only commands and unit tests credential-free.
    Install the matching Microsoft Agent Framework OpenAI provider in the
    execution environment as documented in the README.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key, self.model = api_key, model

    async def run_json(
        self, instructions: str, prompt: str, response_model: type[BaseModel]
    ) -> str:
        try:
            from agent_framework.openai import OpenAIChatClient
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-specific
            raise RuntimeError(
                "Microsoft Agent Framework OpenAI provider is unavailable; "
                "install agent-framework-openai compatible with agent-framework-core."
            ) from exc
        client = OpenAIChatClient(api_key=self.api_key, model=self.model)
        agent = client.as_agent(
            name="VTuberReadingResearch"
            if "ReadingResearchAgent" in instructions
            else "VTuberVerification",
            instructions=instructions,
            tools=[client.get_web_search_tool()],
        )
        response = await agent.run(prompt, options={"response_format": response_model})
        value = getattr(response, "value", None)
        if isinstance(value, BaseModel):
            return value.model_dump_json()
        return str(getattr(response, "text", response))


class MissingOpenAICredentials:
    """Delays a configuration failure until a candidate actually needs research."""

    async def research(self, candidate: Candidate, metrics: AudienceMetrics) -> ResearchResult:
        raise RuntimeError("OPENAI_API_KEY is required once a candidate passes audience thresholds")

    async def verify(
        self, candidate: Candidate, research: ResearchResult, metrics: AudienceMetrics
    ) -> VerificationResult:
        raise RuntimeError("OPENAI_API_KEY is required once a candidate passes audience thresholds")


RESEARCH_INSTRUCTIONS = """You are ReadingResearchAgent. Research exactly one Japanese VTuber.
Use web search where available. Prefer official agency profiles, the creator's official site,
YouTube/Twitch descriptions, then official social accounts. Do not infer a Japanese reading from
kanji or romanisation. Return only a JSON object matching: canonical_name (string|null), reading
(string|null, hiragana only), confidence (0..1), evidence ([{url,source_type,claim}]), and status
(resolved|unresolved). Use source_type values official_agency_profile, official_profile,
official_website, youtube_about, twitch_about, official_social, or other. A non-official wiki
alone cannot resolve a reading."""

VERIFY_INSTRUCTIONS = """You are VerificationAgent, independent of a prior researcher.
Independently use web search to check one VTuber's identity, formal name, reading, and cited URLs.
Do not approve merely because the researcher claims it. Return only a JSON object matching:
verified (boolean), canonical_name (string|null), reading (string|null), confidence (0..1),
evidence ([{url,source_type,claim}]), issues ([string]). Use the same source_type vocabulary as
the research agent and explain disagreement in issues."""


def _candidate_context(candidate: Candidate, metrics: AudienceMetrics) -> str:
    return json.dumps(
        {"candidate": candidate.model_dump(mode="json"), "platform_metadata": metrics.model_dump()},
        ensure_ascii=False,
    )


class ReadingResearchAgent:
    def __init__(self, runner: JsonAgentRunner) -> None:
        self.runner = runner

    async def research(self, candidate: Candidate, metrics: AudienceMetrics) -> ResearchResult:
        raw = await self.runner.run_json(
            RESEARCH_INSTRUCTIONS, _candidate_context(candidate, metrics), ResearchResult
        )
        return ResearchResult.model_validate_json(raw)


class VerificationAgent:
    def __init__(self, runner: JsonAgentRunner) -> None:
        self.runner = runner

    async def verify(
        self, candidate: Candidate, research: ResearchResult, metrics: AudienceMetrics
    ) -> VerificationResult:
        prompt = (
            _candidate_context(candidate, metrics)
            + "\nPrior research to audit (not authority):\n"
            + research.model_dump_json()
        )
        raw = await self.runner.run_json(VERIFY_INSTRUCTIONS, prompt, VerificationResult)
        return VerificationResult.model_validate_json(raw)
