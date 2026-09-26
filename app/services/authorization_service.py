"""
Talos Cloud — Centralized Capability-Based Authorization Service.

Evaluates permissions, execution policies, capabilities, and approval gates.
Rule:
  Authentication answers: "Who are you?"
  Authorization answers:  "What may you do?"

Hierarchy:
  Account -> Device -> Session -> Project -> Agent Run -> Capability -> Tool

Execution Modes:
  - review: read = automatic; mutations / shell / external = approval required
  - sandbox: automatic execution only inside sandbox boundaries
  - trusted: automatic execution according to project policies
  - unrestricted: full local execution authority (explicitly enabled)

Decisions:
  - ALLOW: action permitted automatically
  - REQUIRE_APPROVAL: action requires explicit user confirmation
  - DENY: action prohibited by policy or role
"""

from __future__ import annotations

import enum
import logging
import uuid
from typing import Any, Optional
from pydantic import BaseModel

logger = logging.getLogger("talos.auth.authorization")


class AuthDecision(str, enum.Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class AuthorizationContext(BaseModel):
    account_id: str
    role: str = "user"
    device_id: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    project_role: Optional[str] = None  # owner, admin, editor, viewer
    execution_mode: str = "review"      # review, sandbox, trusted, unrestricted
    scopes: list[str] = []
    run_id: Optional[str] = None


# Actions and Capabilities Mapping
SENSITIVE_ACTIONS = {
    "terminal:execute",
    "shell:run",
    "workspace:delete",
    "filesystem:format",
    "system:modify",
    "credentials:write",
}

READ_ACTIONS = {
    "workspace:read",
    "file:read",
    "file:list",
    "agent:status",
    "marketplace:read",
    "browser:read",
    "search:query",
}

MUTATION_ACTIONS = {
    "workspace:write",
    "file:write",
    "file:edit",
    "file:delete",
    "package:install",
    "marketplace:publish",
    "workflow:execute",
    "agent:run",
}


def authorize(
    ctx: AuthorizationContext,
    action: str,
    resource: Optional[str] = None,
    tool_args: Optional[dict[str, Any]] = None,
) -> tuple[AuthDecision, str]:
    """
    Evaluates policy for a requested action within the identity & project context.
    Returns (AuthDecision, reason).
    """
    # 1. Admin bypass / superuser
    if ctx.role == "admin":
        return AuthDecision.ALLOW, "Admin role permits all operations."

    # 2. Scope enforcement if specific scopes are attached to token/session
    if ctx.scopes and "*" not in ctx.scopes:
        # Check action prefix or direct action match
        action_parts = action.split(":")
        category = action_parts[0] if action_parts else action
        has_scope = any(
            s == action or s == f"{category}:*" or s == "*"
            for s in ctx.scopes
        )
        if not has_scope:
            return AuthDecision.DENY, f"Missing required capability scope for '{action}'."

    # 3. Project role boundaries
    if ctx.project_role == "viewer" and action not in READ_ACTIONS:
        return AuthDecision.DENY, f"Viewer role is restricted to read-only actions."

    # 4. Execution Mode Enforcement
    mode = (ctx.execution_mode or "review").lower()

    if mode == "unrestricted":
        return AuthDecision.ALLOW, "Unrestricted mode permits all operations."

    if mode == "review":
        if action in READ_ACTIONS:
            return AuthDecision.ALLOW, "Read operation permitted automatically in review mode."
        if action in SENSITIVE_ACTIONS or action in MUTATION_ACTIONS:
            return AuthDecision.REQUIRE_APPROVAL, f"Action '{action}' requires user approval in review mode."

    if mode == "sandbox":
        if action in SENSITIVE_ACTIONS:
            return AuthDecision.DENY, f"Action '{action}' is strictly prohibited in sandbox mode."
        return AuthDecision.ALLOW, "Action permitted within sandbox boundaries."

    if mode == "trusted":
        if action in SENSITIVE_ACTIONS:
            return AuthDecision.REQUIRE_APPROVAL, f"Sensitive action '{action}' requires user approval."
        return AuthDecision.ALLOW, "Action permitted in trusted mode."

    return AuthDecision.ALLOW, "Permitted by default policy."
