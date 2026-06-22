"""Approval agent (Qwen3 via Ollama). Generates a summary of the diagnosis + fix for user approval."""
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from schemas import ApprovalSummary

approval_agent = LlmAgent(
    name="approval_agent",
    model=LiteLlm(model="ollama_chat/qwen3"),
    description="Summarizes the error log, issue, category, and fix for user approval.",
    instruction=(
        "You are given the error log, issue, category, and fix. Write a clear, "
        "concise summary covering all four for the user to review and approve."
    ),
    output_schema=ApprovalSummary,
    output_key="approval_summary",
)
