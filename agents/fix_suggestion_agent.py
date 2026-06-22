"""Fix suggestion agents (Ollama / Qwen 3, local — for demo/dev use).

This is the local-model counterpart to the Gemini-based pipeline. Since
Ollama models don't support Gemini's built-in google_search tool, this
version skips live search entirely: the research agent is given the
repo's file listing / relevant file contents directly as context and
reasons over that instead of querying the web. This keeps the two-agent
shape (free-text research -> structured extraction) so it's a drop-in
swap when you want to demo without burning Gemini API quota.
"""
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from schemas import FixSuggestion

# Ollama must be running locally with the model pulled:
#   ollama pull qwen3
#   ollama serve
OLLAMA_MODEL = LiteLlm(model="ollama_chat/qwen3")

fix_research_agent = LlmAgent(
    name="fix_research_agent",
    model=OLLAMA_MODEL,
    description=(
        "Given a classified issue plus the repo's file listing/contents, "
        "writes a plain-text fix proposal with reasoning. No web search; "
        "diagnosis is based entirely on the provided repo context."
    ),
    instruction=(
        "You are given an issue, its category, and the contents of the "
        "repository relevant to that issue (file listing and/or file "
        "contents). Do not assume access to the internet or any tools. "
        "Using only the provided repo context, write a plain-text answer "
        "covering: a concrete fix description (what file to add/change and "
        "what its contents should be), and your reasoning for why this fix "
        "addresses the root cause."
    ),
    output_key="fix_research_text",
)

fix_suggestion_agent = LlmAgent(
    name="fix_suggestion_agent",
    model=OLLAMA_MODEL,
    description="Structures a fix proposal into issue, category, fix, and reasoning fields.",
    instruction=(
        "You are given an issue, category, and a plain-text fix proposal. "
        "Extract and return it as issue, category, fix, and reasoning "
        "fields exactly. Respond with ONLY the structured fields — no "
        "extra commentary, no markdown fences."
    ),
    output_schema=FixSuggestion,
    output_key="fix_suggestion",
)