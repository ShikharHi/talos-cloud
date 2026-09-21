"""
Unit tests for package security validation, zip bomb protection, and streamed verification.
"""

import hashlib
import io
import zipfile
import pytest

from app.storage.package_security import (
    MAX_ENTRY_COUNT,
    sanitize_archive_member_path,
    verify_package_stream,
)
from app.storage.exceptions import StorageValidationError


def _make_zip(files: dict[str, str | bytes]) -> io.BytesIO:
    """Helper to construct in-memory zip bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, str):
                zf.writestr(name, content.encode("utf-8"))
            else:
                zf.writestr(name, content)
    buf.seek(0)
    return buf


def test_valid_agent_package_verification():
    """Tests that a valid agent package with manifest and Python code passes verification."""
    manifest_content = """
name: research-agent
author: alice
version: 1.0.0
display_name: Research Agent
tagline: Autonomous web researcher
kind: agent
"""
    agent_code = """
import os
def run():
    print("Agent running")
"""
    stream = _make_zip({
        "agent.yaml": manifest_content,
        "agent.py": agent_code,
    })

    result = verify_package_stream(stream, resource_type="agent")
    assert result.valid is True
    assert result.file_size > 0
    assert len(result.sha256) == 64
    assert result.manifest_yaml is not None
    assert "research-agent" in result.manifest_yaml


def test_valid_skill_package_verification():
    """Tests that a valid skill package with SKILL.md passes verification."""
    skill_md = """---
name: web_search
description: Search the web
---
# Web Search Skill
"""
    stream = _make_zip({"SKILL.md": skill_md})
    result = verify_package_stream(stream, resource_type="skill")
    assert result.valid is True
    assert result.manifest_yaml is not None
    assert "web_search" in result.manifest_yaml


def test_missing_manifest_rejected():
    """Tests that a package lacking an expected manifest is rejected."""
    stream = _make_zip({"random.txt": "just some text"})
    result = verify_package_stream(stream, resource_type="agent")
    assert result.valid is False
    assert any("missing manifest" in err.lower() for err in result.errors)


def test_path_traversal_archive_member_rejected():
    """Tests that archive entries containing directory traversal are caught and rejected."""
    with pytest.raises(StorageValidationError):
        sanitize_archive_member_path("../../../etc/passwd")

    with pytest.raises(StorageValidationError):
        sanitize_archive_member_path("folder/../../secret.txt")

    with pytest.raises(StorageValidationError):
        sanitize_archive_member_path("/absolute/path/file.txt")


def test_sha256_checksum_verification():
    """Tests server-side SHA256 checksum verification."""
    stream = _make_zip({"SKILL.md": "# Simple Skill"})
    data = stream.getvalue()
    real_hash = hashlib.sha256(data).hexdigest()

    # Correct hash matches
    res_ok = verify_package_stream(io.BytesIO(data), "skill", expected_sha256=real_hash)
    assert res_ok.valid is True
    assert res_ok.sha256 == real_hash

    # Wrong hash fails
    res_bad = verify_package_stream(io.BytesIO(data), "skill", expected_sha256="wronghash00000000000000000000000000000000000000000000000000000000")
    assert res_bad.valid is False
    assert any("checksum mismatch" in err.lower() for err in res_bad.errors)


def test_malformed_corrupt_zip_rejected():
    """Tests that arbitrary or corrupted binary data is cleanly rejected."""
    corrupt_stream = io.BytesIO(b"this is definitely not a zip file")
    result = verify_package_stream(corrupt_stream, "agent")
    assert result.valid is False
    assert any("malformed" in err.lower() or "zip" in err.lower() for err in result.errors)
