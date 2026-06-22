"""Structured output schemas for agent handoffs."""
from typing import Literal

from pydantic import BaseModel

IssueCategory = Literal["dependency error", "build error", "runtime error", "configuration error"]


class DeploymentResult(BaseModel):
    success: bool
    deployment_url: str | None = None
    raw_log_excerpt: str | None = None  # only populated on failure


class ErrorLog(BaseModel):
    error_message: str
    error_summary: str  # concise summary sent on to issue_classification_agent


class IssueClassification(BaseModel):
    issue: str
    category: IssueCategory


class FixSuggestion(BaseModel):
    issue: str
    category: IssueCategory
    fix: str
    reasoning: str


class ValidationResult(BaseModel):
    approved: bool
    reason: str  # why approved, or why rejected (e.g. "fix does not address the root cause")


class ApprovalSummary(BaseModel):
    summary: str


class CodePatch(BaseModel):
    file_path: str
    file_content: str  # full new content of the file
