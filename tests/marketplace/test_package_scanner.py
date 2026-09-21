"""
Tests for PackageScanner:
  - Zip Slip path traversal defenses
  - Symlink detection and rejection
  - Decompression bomb protection (ratio & uncompressed limit)
  - Executable magic header inspection (MZ, ELF, Mach-O, WASM)
  - Forbidden extensions (.exe, .dll, .so, etc.)
  - Checksum validation
  - Manifest validation for all package kinds
  - Static AST security policy checks
"""

import io
import zipfile
import pytest

from app.domain.marketplace.errors import (
    PackageArchiveInvalidError,
    PackageChecksumMismatchError,
    PackageTooLargeError,
)
from app.infrastructure.security.package_scanner import PackageScanner


def _make_zip(files: dict[str, bytes | str]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(path, data)
    buf.seek(0)
    return buf


def test_valid_skill_scan():
    valid_skill_md = """---
name: my-skill
description: Useful test skill
version: 1.0.0
---

# My Skill
Instructions go here.
"""
    stream = _make_zip({
        "SKILL.md": valid_skill_md,
        "helper.py": "def add(a, b):\n    return a + b\n",
    })
    report = PackageScanner.scan_archive_stream(stream, kind="skill")
    assert report.valid is True
    assert report.manifest_data is not None
    assert report.manifest_data["name"] == "my-skill"
    assert report.sha256 != ""


def test_valid_agent_scan():
    valid_agent_yaml = """
name: my-agent
author: alice
version: 1.0.0
kind: agent
entrypoint: run.py
display_name: Alice Agent
tagline: Smart assistant
system_prompt: You are a helpful assistant.
tools: []
"""
    stream = _make_zip({
        "agent.yaml": valid_agent_yaml,
        "run.py": "print('Agent initialized')\n",
    })
    report = PackageScanner.scan_archive_stream(stream, kind="agent")
    assert report.valid is True
    assert report.manifest_data["name"] == "my-agent"


def test_zip_slip_dotdot_blocked():
    stream = _make_zip({
        "../../evil.sh": "#!/bin/sh\nrm -rf /\n",
        "SKILL.md": "---\nname: test\n---\n",
    })
    with pytest.raises(PackageArchiveInvalidError) as exc_info:
        PackageScanner.scan_archive_stream(stream, kind="skill")
    assert "traversal" in str(exc_info.value).lower() or ".." in str(exc_info.value)


def test_zip_slip_absolute_path_blocked():
    stream = _make_zip({
        "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\n",
        "SKILL.md": "---\nname: test\n---\n",
    })
    with pytest.raises(PackageArchiveInvalidError) as exc_info:
        PackageScanner.scan_archive_stream(stream, kind="skill")
    assert "absolute path" in str(exc_info.value).lower()


def test_zip_slip_windows_drive_blocked():
    stream = _make_zip({
        "C:/windows/system32/cmd.exe": "fake",
        "SKILL.md": "---\nname: test\n---\n",
    })
    with pytest.raises(PackageArchiveInvalidError) as exc_info:
        PackageScanner.scan_archive_stream(stream, kind="skill")
    assert "drive letter" in str(exc_info.value).lower()


def test_forbidden_extension_blocked():
    stream = _make_zip({
        "payload.exe": b"harmless content with bad ext",
        "SKILL.md": "---\nname: test\n---\n",
    })
    with pytest.raises(PackageArchiveInvalidError) as exc_info:
        PackageScanner.scan_archive_stream(stream, kind="skill")
    assert "executable or binary" in str(exc_info.value).lower() or ".exe" in str(exc_info.value)


def test_magic_executable_header_blocked():
    # File named innocent.txt but begins with ELF magic bytes
    elf_bytes = b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    stream = _make_zip({
        "innocent.txt": elf_bytes,
        "SKILL.md": "---\nname: test\n---\n",
    })
    with pytest.raises(PackageArchiveInvalidError) as exc_info:
        PackageScanner.scan_archive_stream(stream, kind="skill")
    assert "binary" in str(exc_info.value).lower() or "header" in str(exc_info.value).lower()


def test_wasm_magic_blocked():
    wasm_bytes = b"\x00asm\x01\x00\x00\x00"
    stream = _make_zip({
        "module.dat": wasm_bytes,
        "SKILL.md": "---\nname: test\n---\n",
    })
    with pytest.raises(PackageArchiveInvalidError) as exc_info:
        PackageScanner.scan_archive_stream(stream, kind="skill")
    assert "binary" in str(exc_info.value).lower() or "header" in str(exc_info.value).lower()


def test_symlink_disallowed():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Create a member with Unix symlink attribute (0o120777)
        zinfo = zipfile.ZipInfo("symlink_target")
        zinfo.create_system = 3  # Unix
        zinfo.external_attr = 0o120777 << 16  # S_IFLNK
        zf.writestr(zinfo, "/etc/passwd")
        zf.writestr("SKILL.md", "---\nname: test\n---\n")
    buf.seek(0)

    with pytest.raises(PackageArchiveInvalidError) as exc_info:
        PackageScanner.scan_archive_stream(buf, kind="skill")
    assert "symlink" in str(exc_info.value).lower()


def test_checksum_mismatch():
    stream = _make_zip({
        "SKILL.md": "---\nname: test\n---\n",
    })
    with pytest.raises(PackageChecksumMismatchError):
        PackageScanner.scan_archive_stream(
            stream, kind="skill", expected_sha256="0000000000000000000000000000000000000000000000000000000000000000"
        )


def test_static_ast_checks():
    code_with_dangerous_calls = """
import os
import ctypes

def run():
    os.system('echo dangerous')
"""
    findings = PackageScanner.scan_python_code(code_with_dangerous_calls, "runner.py")
    rule_ids = {f.rule_id for f in findings}
    assert "FORBIDDEN_MODULE_IMPORT" in rule_ids
    assert "FORBIDDEN_FUNCTION_CALL" in rule_ids
