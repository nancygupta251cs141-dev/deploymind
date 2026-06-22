"""Issue classification agent (Qwen3 via Ollama). Categorizes the issue into one of 4 fixed categories."""
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from schemas import IssueClassification

issue_classification_agent = LlmAgent(
    name="issue_classification_agent",
    model=LiteLlm(model="ollama_chat/qwen3"),
    description="Categorizes an error summary into one of: dependency error, build error, runtime error, configuration error.",
    instruction=(
        "You are given a concise error summary. Identify the specific issue, then "
        "classify it into exactly one of these categories: 'dependency error', "
        "'build error', 'runtime error', 'configuration error'. Return the issue "
        "description and category."
    ),
    output_schema=IssueClassification,
    output_key="issue_classification",
)
