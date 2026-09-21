"""
Talos Cloud — Package Security & Archive Verification.

IMPORTANT:
Marketplace static security validation is not antivirus protection and is not an
OS/runtime sandbox. Untrusted package execution requires a separate execution sandbox.
This scanner performs structural archive integrity checks, path traversal defenses,
manifest contract validation, and static AST policy analysis.
"""

from __future__ import annotations

import ast
import hashlib
import io
import os
import zipfile
from pathlib import Path
from typing import BinaryIO, Optional, Set

from app.domain.marketplace.errors import (
    PackageArchiveInvalidError,
    PackageChecksumMismatchError,
    PackageTooLargeError,
)
from app.domain.marketplace.manifest import parse_and_validate_manifest
from app.domain.marketplace.security import (
    FindingSeverity,
    MAX_COMPRESSION_RATIO,
    MAX_ENTRY_COUNT,
    MAX_INDIVIDUAL_FILE_BYTES,
    MAX_PACKAGE_SIZE_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    SecurityFinding,
    SecurityScanReport,
)

# Known dangerous compiled executable signatures
DANGEROUS_MAGIC_PREFIXES = (
    b"MZ",                       # DOS/PE EXE/DLL
    b"\x7fELF",                  # Linux ELF
    b"\xfe\xed\xfa\xce",         # Mach-O 32-bit (BE)
    b"\xce\xfa\xed\xfe",         # Mach-O 32-bit (LE)
    b"\xfe\xed\xfa\xcf",         # Mach-O 64-bit (BE)
    b"\xcf\xfa\xed\xfe",         # Mach-O 64-bit (LE)
    b"\xca\xfe\xba\xbe",         # Java class / Mach-O Fat
    b"\x00asm",                  # WebAssembly binary
)

FORBIDDEN_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".com", ".scr", ".msi", ".cpl", ".pif"
}

FORBIDDEN_AST_MODULES = {"ctypes", "pty", "subprocess", "socket"}
FORBIDDEN_AST_CALLS = {"os.system", "os.popen", "os.exec", "os.spawn"}

MANIFEST_FILENAMES = {
    "agent": ("agent.yaml", "agent.yml"),
    "skill": ("SKILL.md", "skill.md", "skill.yaml", "skill.yml"),
    "mcp": ("mcp.yaml", "mcp.yml"),
    "tool": ("tool.yaml", "tool.yml"),
}


class PackageScanner:
    """
    Central package verification engine used by the Celery package-security task.
    """

    @classmethod
    def sanitize_member_path(cls, filename: str) -> str:
        """
        Validates zip member paths against traversal attacks, absolute paths,
        NUL bytes, and Windows device/drive syntax.
        """
        if "\0" in filename:
            raise PackageArchiveInvalidError(f"Archive entry contains NUL byte: {filename!r}")

        clean = filename.replace("\\", "/")

        if clean.startswith("/"):
            raise PackageArchiveInvalidError(f"Archive entry contains absolute path: {filename!r}")

        if len(clean) >= 2 and clean[1] == ":":
            raise PackageArchiveInvalidError(f"Archive entry contains Windows drive letter: {filename!r}")

        parts = [p for p in clean.split("/") if p]
        for part in parts:
            if part == "..":
                raise PackageArchiveInvalidError(f"Archive entry contains path traversal '..': {filename!r}")

        return "/".join(parts)

    @classmethod
    def scan_python_code(cls, source_code: str, filename: str) -> list[SecurityFinding]:
        findings: list[SecurityFinding] = []
        try:
            tree = ast.parse(source_code)
        except SyntaxError as e:
            findings.append(
                SecurityFinding(
                    rule_id="PYTHON_SYNTAX_ERROR",
                    severity=FindingSeverity.WARNING,
                    message=f"Syntax error during static analysis: {e}",
                    file=filename,
                    line=e.lineno,
                    category="ast",
                )
            )
            return findings

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    if root_pkg in FORBIDDEN_AST_MODULES:
                        findings.append(
                            SecurityFinding(
                                rule_id="FORBIDDEN_MODULE_IMPORT",
                                severity=FindingSeverity.WARNING,
                                message=f"Import of unconfined module '{alias.name}' detected.",
                                file=filename,
                                line=getattr(node, "lineno", None),
                                category="static_policy",
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_pkg = node.module.split(".")[0]
                    if root_pkg in FORBIDDEN_AST_MODULES:
                        findings.append(
                            SecurityFinding(
                                rule_id="FORBIDDEN_MODULE_IMPORT",
                                severity=FindingSeverity.WARNING,
                                message=f"Import from unconfined module '{node.module}' detected.",
                                file=filename,
                                line=getattr(node, "lineno", None),
                                category="static_policy",
                            )
                        )
            elif isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        func_name = f"{node.func.value.id}.{node.func.attr}"
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id

                for bad in FORBIDDEN_AST_CALLS:
                    if func_name.startswith(bad):
                        findings.append(
                            SecurityFinding(
                                rule_id="FORBIDDEN_FUNCTION_CALL",
                                severity=FindingSeverity.WARNING,
                                message=f"Call to potentially dangerous system function '{func_name}' detected.",
                                file=filename,
                                line=getattr(node, "lineno", None),
                                category="static_policy",
                            )
                        )

        return findings

    @classmethod
    def scan_archive_stream(
        cls,
        stream_file: BinaryIO,
        kind: str,
        expected_sha256: Optional[str] = None,
        max_size: int = MAX_PACKAGE_SIZE_BYTES,
    ) -> SecurityScanReport:
        """
        Performs streaming verification of package archive:
        1. SHA-256 calculation
        2. Size & entry count validation
        3. Decompression bomb checks
        4. Zip Slip path traversal checks
        5. Executable header detection
        6. Manifest parsing & schema validation
        7. Static Python AST scanning
        """
        stream_file.seek(0)
        hasher = hashlib.sha256()
        total_size = 0

        while True:
            chunk = stream_file.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            total_size += len(chunk)
            if total_size > max_size:
                raise PackageTooLargeError(
                    f"Package size ({total_size} bytes) exceeds maximum limit ({max_size} bytes)."
                )

        calculated_sha256 = hasher.hexdigest()

        if expected_sha256 and calculated_sha256.lower() != expected_sha256.strip().lower():
            raise PackageChecksumMismatchError(
                f"Checksum mismatch: expected '{expected_sha256}', calculated '{calculated_sha256}'."
            )

        if total_size == 0:
            raise PackageArchiveInvalidError("Uploaded package archive is empty (0 bytes).")

        # Open zip
        stream_file.seek(0)
        try:
            zf = zipfile.ZipFile(stream_file, "r")
        except zipfile.BadZipFile as e:
            raise PackageArchiveInvalidError(f"Malformed or corrupted ZIP archive: {e}")

        infolist = zf.infolist()
        if not infolist:
            raise PackageArchiveInvalidError("Package archive contains 0 files.")

        if len(infolist) > MAX_ENTRY_COUNT:
            raise PackageArchiveInvalidError(
                f"Archive entry count ({len(infolist)}) exceeds maximum allowed ({MAX_ENTRY_COUNT})."
            )

        total_uncompressed = 0
        findings: list[SecurityFinding] = []
        manifest_raw: Optional[str] = None
        manifest_name: str = ""

        expected_names = MANIFEST_FILENAMES.get(kind.lower(), ("manifest.yaml", "SKILL.md"))

        for info in infolist:
            # 1. Path safety check (Zip Slip)
            clean_path = cls.sanitize_member_path(info.filename)

            # 2. Check symlinks (Unix symlink attribute in zip)
            # High 16 bits of external_attr store Unix file mode
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if (unix_mode & 0o170000) == 0o120000:
                raise PackageArchiveInvalidError(f"Symlinks are forbidden in marketplace archives: {info.filename}")

            # 3. Extension check
            ext = Path(clean_path).suffix.lower()
            if ext in FORBIDDEN_EXTENSIONS:
                raise PackageArchiveInvalidError(f"Forbidden binary file extension '{ext}' in {info.filename}")

            if info.is_dir():
                continue

            # 4. Check individual file size limit
            if info.file_size > MAX_INDIVIDUAL_FILE_BYTES:
                raise PackageTooLargeError(
                    f"File '{info.filename}' size ({info.file_size} bytes) exceeds limit ({MAX_INDIVIDUAL_FILE_BYTES})."
                )

            total_uncompressed += info.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise PackageTooLargeError(
                    f"Total uncompressed archive size exceeds limit ({MAX_UNCOMPRESSED_BYTES} bytes)."
                )

            # 5. Check manifest candidates
            path_parts = [p for p in clean_path.split("/") if p]
            if len(path_parts) in (1, 2) and any(path_parts[-1].lower() == m.lower() for m in expected_names):
                if not manifest_raw:
                    try:
                        with zf.open(info) as mf:
                            manifest_raw = mf.read(1_048_576).decode("utf-8", errors="replace")
                            manifest_name = path_parts[-1]
                    except Exception as e:
                        findings.append(
                            SecurityFinding(
                                rule_id="MANIFEST_READ_FAILED",
                                severity=FindingSeverity.ERROR,
                                message=f"Failed to read candidate manifest: {e}",
                                file=info.filename,
                            )
                        )

            # 6. Check magic headers and scan python files
            if info.file_size > 0:
                with zf.open(info) as f:
                    header = f.read(16)
                    for magic in DANGEROUS_MAGIC_PREFIXES:
                        if header.startswith(magic):
                            raise PackageArchiveInvalidError(
                                f"Executable binary header detected in file '{info.filename}'."
                            )

                    # Scan Python files for AST policy
                    if ext == ".py" and info.file_size < 2_000_000:
                        f.seek(0)
                        py_code = f.read().decode("utf-8", errors="replace")
                        findings.extend(cls.scan_python_code(py_code, info.filename))

        # Check compression ratio (Zip bomb)
        if total_size > 0:
            ratio = total_uncompressed / total_size
            if ratio > MAX_COMPRESSION_RATIO:
                raise PackageArchiveInvalidError(
                    f"Archive compression ratio ({ratio:.1f}:1) exceeds limit ({MAX_COMPRESSION_RATIO}:1)."
                )

        # Validate manifest
        if not manifest_raw:
            raise PackageArchiveInvalidError(
                f"Required manifest file ({', '.join(expected_names)}) not found in package root."
            )

        parsed_manifest = parse_and_validate_manifest(kind, manifest_raw, filename=manifest_name)

        return SecurityScanReport(
            valid=True,
            sha256=calculated_sha256,
            file_size=total_size,
            uncompressed_size=total_uncompressed,
            entry_count=len(infolist),
            manifest_data=parsed_manifest.model_dump(),
            findings=findings,
        )
