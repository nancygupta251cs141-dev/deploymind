"""Patch generation agent. Converts an approved fix description into an actual
file_path/file_content edit, used right before pushing to main.

_patch_editing_agent runs on Gemini 2.5 Flash Lite (better reasoning for deciding
which file to touch and what the fix should contain). _patch_structuring_agent stays
on Qwen3 via Ollama (pure text->schema formatting, no real reasoning needed, so the
cheaper local model is fine here and keeps API usage down).

Note: ADK does not allow tools and output_schema on the same LlmAgent — that's still
true on either model — so this stays split into two LLM calls wrapped in a
SequentialAgent. The public object is still named patch_generation_agent and still
produces output_key="code_patch", same as before. Nothing importing this module
needs to change.
"""
import os

from github import Github, GithubException
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm
from schemas import CodePatch

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
BASE_BRANCH = os.environ.get("BASE_BRANCH", "main")

# Ollama must be running locally with the model pulled:
#   ollama pull qwen3
#   ollama serve
QWEN_MODEL = LiteLlm(model="ollama_chat/qwen3")


def read_repo_file(file_path: str) -> str:
    """Reads a file's current content from the base branch, so the model can edit it in
    place. If the file does not exist, returns a clear marker instead of raising, so the
    model can create the file fresh rather than crash the agent on a 404.
    """
    gh = Github(GITHUB_TOKEN)
    repo = gh.get_repo(GITHUB_REPO)
    try:
        content_file = repo.get_contents(file_path, ref=BASE_BRANCH)
        return content_file.decoded_content.decode("utf-8")
    except GithubException as e:
        if e.status == 404:
            return "FILE_NOT_FOUND: this file does not exist in the repo yet."
        raise


def list_repo_root_files() -> list[str]:
    """Lists file/directory names at the repo root, so the model can pick a real
    existing file instead of guessing a name that may not exist in this project.
    """
    gh = Github(GITHUB_TOKEN)
    repo = gh.get_repo(GITHUB_REPO)
    contents = repo.get_contents("", ref=BASE_BRANCH)
    return [c.path for c in contents]


_patch_editing_agent = LlmAgent(
    name="patch_editing_agent",
    model="gemini-2.5-flash-lite",
    description="Converts a fix description and reasoning into a concrete file_path/file_content edit.",
    instruction=(
        "You are given a fix description, its reasoning, and a category — one of: "
        "'dependency error', 'build error', 'runtime error', 'configuration error'.\n\n"

        "STEP 1 — REQUIRED: Call list_repo_root_files now, before writing anything. "
        "Do not assume a file exists just because it's common in other projects.\n\n"

        "STEP 2 — REQUIRED: Decide which single file needs to change to apply the "
        "fix. STRONG DEFAULT: edit an existing file. Only propose creating a new "
        "file if, after reviewing the full list from Step 1, you can name which "
        "existing files you considered and explain why none of them serve this "
        "purpose. If you cannot name files you ruled out, do not create a new file "
        "— re-examine the list instead. Call read_repo_file on the file you pick "
        "(and on any other file that helps you understand the actual cause). "
        "'FILE_NOT_FOUND' only applies if you've justified that no existing file "
        "fits — it is not an excuse to skip checking.\n\n"

        "STEP 3 — Write the fix, guided by the category:\n"
        "- 'dependency error': the file is usually a manifest (package.json, "
        "requirements.txt, etc). It must be complete and runnable, not a minimal "
        "stub — e.g. for package.json, include name, version, main, scripts.start, "
        "and dependencies (scan every entry-point file you read for require(...) "
        "or import ... from \"...\" statements and include EVERY non-relative, "
        "non-built-in package — do not omit any, and never return a file with "
        "only an engines field, since that alone does not fix a missing manifest).\n"
        "- 'build error': look for the actual cause in the error/reasoning given — "
        "a misconfigured build script, wrong entry point, bad path, or syntax issue "
        "in a build config file (e.g. tsconfig.json, webpack config, Dockerfile). "
        "Fix the specific line(s) causing the failure; do not rewrite the whole "
        "build setup unless the reasoning says the whole thing is broken.\n"
        "- 'runtime error': the fix is almost always inside actual source code "
        "(e.g. a null check, wrong variable, bad function call). Read the file "
        "where the error reasoning says it occurs, and change only what's needed "
        "to address the root cause — preserve all unrelated code exactly as is.\n"
        "- 'configuration error': the fix is almost always editing a config/env "
        "file that ALREADY EXISTS in this repo (e.g. .env, config.json, "
        "docker-compose.yml). You saw the full file list in Step 1 — look through "
        "it carefully for any existing config file before concluding none fit. "
        "Only create a new config file if the fix description explicitly says no "
        "such file exists yet and one must be added.\n\n"

        "In every case: make the SMALLEST correct change that fully fixes the "
        "root cause. Do not rewrite unrelated parts of the file. If the file "
        "already exists, base file_content on its real current content (from "
        "read_repo_file), not a fresh rewrite from scratch.\n\n"

        "STEP 4 — REQUIRED SELF-CHECK before responding: does your answer match "
        "the category's fix pattern above? Does it address the root cause in the "
        "reasoning, not just a surface symptom? Did you actually justify creating "
        "a new file if you did so? If anything is off, fix it now before "
        "responding.\n\n"

        "Return your answer in exactly this shape, nothing else:\n"
        "FILE_PATH: <path>\n"
        "```\n<the FULL new file content with the fix applied>\n```"
    ),
    tools=[read_repo_file, list_repo_root_files],
    output_key="_patch_edit_text",
)

_patch_structuring_agent = LlmAgent(
    name="patch_structuring_agent",
    model=QWEN_MODEL,
    description="Structures a FILE_PATH + code block into file_path/file_content fields.",
    instruction=(
        "You are given text with a FILE_PATH line and a fenced code block. "
        "Extract file_path and file_content (the code block's exact contents, "
        "no fence markers). Respond with ONLY the structured fields."
    ),
    output_schema=CodePatch,
    output_key="code_patch",
)

# Public name unchanged: orchestrator.py imports this exact name and reads
# state["code_patch"] from it, same as the original single-agent version.
patch_generation_agent = SequentialAgent(
    name="patch_generation_agent",
    sub_agents=[_patch_editing_agent, _patch_structuring_agent],
)