"""Log analysis agent (Qwen3 via Ollama). Extracts the last error and a concise summary."""
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from schemas import ErrorLog

log_analysis_agent = LlmAgent(
    name="log_analysis_agent",
    model=LiteLlm(model="ollama_chat/qwen3"),
    description="Reads deployment logs, finds the last error, and writes a concise summary of it.",
    instruction=(
        "You are given a deployment log excerpt in the conversation. "
        "Find the last/most relevant error message in it. Then write a concise "
        "(1-3 sentence) summary of that error suitable for handing to an issue "
        "classification step. Return error_message and error_summary."
    ),
    output_schema=ErrorLog,
    output_key="error_log",
)