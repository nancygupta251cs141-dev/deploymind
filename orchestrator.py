"""Orchestrator: drives the deployment-diagnosis flow.

Control flow (branching + retry loop) lives here in plain Python, not in any
LlmAgent's own reasoning, so the validation retry limit is actually enforced
and the cycle (validation rejects -> log_analysis_agent again) is deterministic.
"""
import asyncio
import os

from dotenv import load_dotenv
load_dotenv()

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agents.deployment_agent import deployment_agent, trigger_deployment
from agents.log_analysis_agent import log_analysis_agent
from agents.issue_classification_agent import issue_classification_agent
from agents.fix_suggestion_agent import fix_research_agent, fix_suggestion_agent
from agents.validation_agent import validation_agent
from agents.approval_agent import approval_agent
from agents.patch_generation_agent import patch_generation_agent
from memory_store import retrieve_similar_fixes, save_fix, format_memory_context

APP_NAME = "deploymind"
USER_ID = "user"
MAX_VALIDATION_ATTEMPTS = 3


async def _run_agent(agent, session_service, session_id: str, message: str) -> dict:
    """Runs one agent turn and returns the session state after it finishes."""
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=message)])
    async for _ in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=content):
        pass  # state is written via output_key as events are processed
    session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    return session.state


async def run_pipeline(repo_url: str) -> dict:
    session_service = InMemorySessionService()
    session = await session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    session_id = session.id

    # 1. Initial deployment (deploys the base branch of the repo configured via env vars)
    # Called directly (not via LlmAgent) — this is a deterministic API call, no reasoning
    # needed, and routing it through an LLM risks hallucinated results instead of real ones.
    deployment_result = trigger_deployment(
        target=os.environ.get('BASE_BRANCH', 'main'), mode="initial"
    )

    if deployment_result["success"]:
        return {"status": "no_error", "message": "No error found."}

    # 2. Error found -> log analysis
    log_excerpt = deployment_result["raw_log_excerpt"]
    validation_attempts = 0

    while True:
        # 3. Log analysis: find last error + concise summary
        state = await _run_agent(
            log_analysis_agent, session_service, session_id,
            f"log:\n{log_excerpt}",
        )
        error_log = state["error_log"]

        # 4. Issue classification (4 fixed categories)
        state = await _run_agent(
            issue_classification_agent, session_service, session_id,
            f"error_summary={error_log['error_summary']}",
        )
        issue_classification = state["issue_classification"]

        # 5. Check memory for similar past fixes before researching from scratch
        past_fixes = retrieve_similar_fixes(
            error_message=error_log["error_message"],
            category=issue_classification["category"],
        )
        memory_context = format_memory_context(past_fixes)

        # Fix research (google_search) then structuring (output_schema) — Gemini
        # does not allow built-in tools and function-calling/output_schema together.
        research_state = await _run_agent(
            fix_research_agent, session_service, session_id,
            (
                f"issue={issue_classification['issue']}\n"
                f"category={issue_classification['category']}\n"
                + (f"\n{memory_context}\n" if memory_context else "")
            ),
        )
        fix_research_text = research_state["fix_research_text"]
        state = await _run_agent(
            fix_suggestion_agent, session_service, session_id,
            (
                f"issue={issue_classification['issue']}\n"
                f"category={issue_classification['category']}\n"
                f"fix_proposal={fix_research_text}"
            ),
        )
        fix_suggestion = state["fix_suggestion"]

        # 6. Validation: review the fix for relevance/hallucination risk (no push, no deploy here)
        validation_attempts += 1
        if validation_attempts > MAX_VALIDATION_ATTEMPTS:
            return {
                "status": "validation_limit_reached",
                "message": f"Validation loop exceeded {MAX_VALIDATION_ATTEMPTS} attempts.",
                "last_issue": issue_classification,
                "last_fix": fix_suggestion,
            }

        state = await _run_agent(
            validation_agent, session_service, session_id,
            (
                f"issue={fix_suggestion['issue']}\n"
                f"category={fix_suggestion['category']}\n"
                f"fix={fix_suggestion['fix']}\n"
                f"reasoning={fix_suggestion['reasoning']}"
            ),
        )
        validation_result = state["validation_result"]

        if validation_result["approved"]:
            # Save the validated fix to memory so future runs can reuse it
            save_fix(
                error_message=error_log["error_message"],
                category=issue_classification["category"],
                fix=fix_suggestion["fix"],
                reasoning=fix_suggestion["reasoning"],
            )
            break  # fix passed review -> go to approval
        else:
            # rejection -> log analysis is called again with the same original error log
            continue

    # 7. Approval summary (for the PR description) + auto-create branch + PR.
    # Validation already approved the fix, so the PR itself is the approval surface:
    # review and merge it on GitHub instead of calling a separate Python function.
    state = await _run_agent(
        approval_agent, session_service, session_id,
        (
            f"error_log={error_log}\n"
            f"issue={issue_classification['issue']}\n"
            f"category={issue_classification['category']}\n"
            f"fix={fix_suggestion['fix']}\n"
            f"reasoning={fix_suggestion['reasoning']}"
        ),
    )
    approval_summary = state["approval_summary"]

    pr_result = await _create_fix_pr(
        session_service, session_id,
        fix_description=fix_suggestion["fix"],
        reasoning=fix_suggestion["reasoning"],
        category=issue_classification["category"],
        summary=approval_summary["summary"],
    )

    return {
        "status": "pr_created",
        "summary": approval_summary["summary"],
        "issue": issue_classification["issue"],
        "category": issue_classification["category"],
        "fix": fix_suggestion["fix"],
        "reasoning": fix_suggestion["reasoning"],
        "pr_url": pr_result["pr_url"],
        "branch_name": pr_result["branch_name"],
    }


async def _create_fix_pr(session_service, session_id: str, fix_description: str, reasoning: str, category: str, summary: str) -> dict:
    """Generates a concrete patch for the validated fix, pushes it to a new branch,
    and opens a PR. Approval now happens by reviewing/merging this PR on GitHub,
    instead of a separate manual Python call.
    """
    import time as _time
    from github import Github, GithubException

    state = await _run_agent(
        patch_generation_agent, session_service, session_id,
        f"fix={fix_description}\nreasoning={reasoning}\ncategory={category}",
    )
    code_patch = state["code_patch"]

    gh = Github(os.environ["GITHUB_TOKEN"])
    repo = gh.get_repo(os.environ["GITHUB_REPO"])
    base_branch = os.environ.get("BASE_BRANCH", "main")

    branch_name = f"deploymind-fix-{int(_time.time())}"
    base_ref = repo.get_branch(base_branch)
    repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_ref.commit.sha)

    commit_message = f"DeployMind fix: {fix_description[:72]}"
    try:
        existing = repo.get_contents(code_patch["file_path"], ref=branch_name)
        repo.update_file(code_patch["file_path"], commit_message, code_patch["file_content"], existing.sha, branch=branch_name)
    except GithubException:
        repo.create_file(code_patch["file_path"], commit_message, code_patch["file_content"], branch=branch_name)

    pr = repo.create_pull(
        title=f"DeployMind: {fix_description[:60]}",
        body=f"**Summary**\n\n{summary}\n\n**Category:** {category}\n\n**Reasoning**\n\n{reasoning}",
        head=branch_name,
        base=base_branch,
    )
    return {"pr_url": pr.html_url, "branch_name": branch_name}


if __name__ == "__main__":
    result = asyncio.run(run_pipeline("https://github.com/example/repo"))
    print(result)