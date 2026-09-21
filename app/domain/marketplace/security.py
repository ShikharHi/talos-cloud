"""
Talos Cloud — Marketplace Security Models & Policy Thresholds.

IMPORTANT SECURITY PRINCIPLE:
Marketplace static security validation is not antivirus protection and is not an
OS/runtime sandbox. Untrusted package execution requires a separate execution sandbox.
Static AST analysis detects known policy violations, but does not prove arbitrary code is safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional


class FindingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class SecurityFinding:
    rule_id: str
    severity: FindingSeverity
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    category: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "category": self.category,
        }


@dataclass
class SecurityScanReport:
    valid: bool
    sha256: str
    file_size: int
    uncompressed_size: int
    entry_count: int
    manifest_data: Optional[dict[str, Any]] = None
    findings: List[SecurityFinding] = field(default_factory=list)
    failure_reason: Optional[str] = None

    @property
    def has_critical_findings(self) -> bool:
        return any(f.severity in (FindingSeverity.ERROR, FindingSeverity.CRITICAL) for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "sha256": self.sha256,
            "file_size": self.file_size,
            "uncompressed_size": self.uncompressed_size,
            "entry_count": self.entry_count,
            "manifest_data": self.manifest_data,
            "findings": [f.to_dict() for f in self.findings],
            "failure_reason": self.failure_reason,
        }


# Centralized Policy Thresholds
MAX_PACKAGE_SIZE_BYTES = int(os.environ.get("TALOS_MAX_PACKAGE_SIZE", 52_428_800))       # 50 MB
MAX_UNCOMPRESSED_BYTES = int(os.environ.get("TALOS_MAX_UNCOMPRESSED_SIZE", 209_715_200)) # 200 MB
MAX_COMPRESSION_RATIO = float(os.environ.get("TALOS_MAX_COMPRESSION_RATIO", 100.0))       # 100:1
MAX_ENTRY_COUNT = int(os.environ.get("TALOS_MAX_ENTRY_COUNT", 2000))                     # 2000 files
MAX_MANIFEST_SIZE_BYTES = int(os.environ.get("TALOS_MAX_MANIFEST_SIZE", 1_048_576))     # 1 MB
MAX_INDIVIDUAL_FILE_BYTES = int(os.environ.get("TALOS_MAX_FILE_SIZE", 52_428_800))       # 50 MB
