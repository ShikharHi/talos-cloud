"""
Talos Cloud — Typed Marketplace Package Manifest Models.

Provides structured schema validation for skill, agent, mcp, and tool manifests.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

from app.domain.marketplace.errors import ManifestInvalidError


class BaseManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, max_length=100)
    version: str = Field(default="1.0.0", max_length=50)
    description: str = Field(default="", max_length=1000)
    author: Optional[str] = Field(default=None, max_length=100)
    tags: List[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        clean = v.strip()
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,99}$", clean):
            raise ValueError("Name must be alphanumeric with hyphens, underscores, or dots.")
        return clean


class SkillManifest(BaseManifest):
    """Manifest for Skills (SKILL.md frontmatter or skill.yaml)."""
    kind: str = "skill"
    instructions: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)


class AgentManifest(BaseManifest):
    """Manifest for Autonomous Agents (agent.yaml)."""
    kind: str = "agent"
    entrypoint: str = Field(..., description="Path to agent entrypoint script, e.g. agent.py")
    runtime: str = Field(default="python", description="Runtime environment, e.g. python, node")
    tools: List[str] = Field(default_factory=list)
    capabilities: List[str] = Field(default_factory=list)
    auth: Optional[dict[str, Any]] = None
    model: Optional[dict[str, Any]] = None


class MCPManifest(BaseManifest):
    """Manifest for Model Context Protocol Connectors (mcp.yaml)."""
    kind: str = "mcp"
    transport: str = Field(default="stdio", description="Transport type: stdio, sse, websocket")
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    env: dict[str, str] = Field(default_factory=dict)
    auth: Optional[dict[str, Any]] = None


class ToolManifest(BaseManifest):
    """Manifest for Tools (tool.yaml)."""
    kind: str = "tool"
    entrypoint: str = Field(..., description="Path to tool module, e.g. tool.py")
    parameters: Optional[dict[str, Any]] = None
    returns: Optional[dict[str, Any]] = None


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter enclosed in --- blocks."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            body = parts[2].strip()
            try:
                data = yaml.safe_load(fm_text) or {}
                if isinstance(data, dict):
                    return data, body
            except Exception as e:
                raise ManifestInvalidError(f"Failed to parse YAML frontmatter: {e}")
    # Fallback to pure YAML
    try:
        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return data, ""
    except Exception as e:
        raise ManifestInvalidError(f"Failed to parse YAML manifest: {e}")
    return {}, text


def parse_and_validate_manifest(
    kind: str, raw_text: str, filename: str = "manifest.yaml"
) -> BaseManifest:
    """
    Parses manifest content and validates against the typed schema for the given kind.
    """
    k = kind.strip().lower()
    if k in ("agent", "agents"):
        k = "agent"
    elif k in ("skill", "skills"):
        k = "skill"
    elif k in ("tool", "tools"):
        k = "tool"
    elif k in ("mcp", "connectors"):
        k = "mcp"

    data, body = parse_frontmatter(raw_text)
    if not data:
        # If markdown without frontmatter, extract basic title from first heading
        if filename.endswith(".md"):
            lines = raw_text.splitlines()
            title = ""
            desc = ""
            for line in lines:
                if line.startswith("# ") and not title:
                    title = line[2:].strip()
                elif line.strip() and not desc and not line.startswith("#"):
                    desc = line.strip()
            data = {"name": title or "custom-skill", "description": desc or "Talos Skill"}
        else:
            raise ManifestInvalidError(f"Empty or invalid manifest for {kind}.")

    if k == "skill":
        if body and "instructions" not in data:
            data["instructions"] = body
        try:
            return SkillManifest.model_validate(data)
        except Exception as e:
            raise ManifestInvalidError(f"Invalid Skill manifest: {e}")
    elif k == "agent":
        try:
            return AgentManifest.model_validate(data)
        except Exception as e:
            raise ManifestInvalidError(f"Invalid Agent manifest: {e}")
    elif k == "mcp":
        try:
            return MCPManifest.model_validate(data)
        except Exception as e:
            raise ManifestInvalidError(f"Invalid MCP manifest: {e}")
    elif k == "tool":
        try:
            return ToolManifest.model_validate(data)
        except Exception as e:
            raise ManifestInvalidError(f"Invalid Tool manifest: {e}")
    else:
        try:
            return BaseManifest.model_validate(data)
        except Exception as e:
            raise ManifestInvalidError(f"Invalid manifest: {e}")
