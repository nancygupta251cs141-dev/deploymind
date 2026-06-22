"""Validation agent (Qwen3 via Ollama). Reviews the suggested fix for relevance and hallucination risk.

This agent does not push code or trigger deployments — it only judges the fix.
The orchestrator routes the result: approved -> approval_agent, rejected -> log_analysis_agent again.
The retry limit is enforced by the orchestrator in Python, not by this agent.
"""
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from schemas import ValidationResult

validation_agent = LlmAgent(
    name="validation_agent",
    model=LiteLlm(model="ollama_chat/qwen3"),
    description="Checks whether a suggested fix is relevant and not hallucinated, given the issue and category.",
    instruction=(
        "You are given an issue, its category, a suggested fix, and the reasoning "
        "behind it. Critically check: does the fix actually address the stated "
        "issue? Is the reasoning plausible, or does it reference things "
        "(files, APIs, config keys, packages) that seem invented or unrelated to "
        "the category? Return approved=true only if the fix is relevant and "
        "well-grounded. Otherwise return approved=false with a clear reason."
    ),
    output_schema=ValidationResult,
    output_key="validation_result",
)
