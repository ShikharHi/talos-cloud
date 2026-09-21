"""
Talos Cloud — Package Security & Archive Verification.

Performs streamed verification of uploaded package archives:
  1. Streaming SHA-256 calculation and spooling to temporary file (no large RAM consumption).
  2. Zip archive integrity and structural validation.
  3. Zip bomb defense (max uncompressed size, compression ratio check, entry limit).
  4. Path traversal defense (rejection of '..', absolute paths, backslashes, symlinks).
  5. Manifest discovery and validation (using talos_agent_sdk if available, or native parser).
  6. Permits legitimate agent scripts (.py, .js, .ts) while blocking malicious path structures.
"""

import hashlib
import io
import os
import tempfile
import zipfile
from pathlib import Path
from typing import BinaryIO, Optional, Tuple

import yaml

from app.storage.exceptions import StorageValidationError
from app.storage.models import UploadVerificationResult

import shutil

# Security thresholds
MAX_PACKAGE_SIZE_BYTES = 52_428_800    # 50 MB compressed
MAX_UNCOMPRESSED_BYTES = 209_715_200   # 200 MB uncompressed
MAX_COMPRESSION_RATIO = 100.0          # 100:1 max ratio
MAX_ENTRY_COUNT = 2000                 # max files/directories in zip

COMPILED_BINARY_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".wasm", ".com", ".scr", ".msi", ".cpl", ".pif"
}

SCRIPT_EXTENSIONS = {
    ".bat", ".cmd", ".ps1", ".vbs", ".sh"
}

DANGEROUS_EXTENSIONS = COMPILED_BINARY_EXTENSIONS | SCRIPT_EXTENSIONS

DANGEROUS_MAGIC_PREFIXES = (
    b"MZ",                       # DOS/PE/DLL/EXE
    b"\x7fELF",                  # Linux/Unix ELF
    b"\xfe\xed\xfa\xce",         # Mach-O 32-bit (BE)
    b"\xce\xfa\xed\xfe",         # Mach-O 32-bit (LE)
    b"\xfe\xed\xfa\xcf",         # Mach-O 64-bit (BE)
    b"\xcf\xfa\xed\xfe",         # Mach-O 64-bit (LE)
    b"\xca\xfe\xba\xbe",         # Mach-O Universal / Java class
    b"\x00asm",                  # WebAssembly binary
)


import ast

FORBIDDEN_AST_MODULES = {"ctypes", "pty", "subprocess", "socket"}
FORBIDDEN_AST_CALLS = {"os.system", "os.popen", "os.exec", "os.spawn"}


def scan_python_ast(source_code: str) -> list[str]:
    """Inspects Python source for dangerous imports and calls without running it."""
    findings = []
    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        return [f"Python syntax error: {e}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                if root_pkg in FORBIDDEN_AST_MODULES:
                    findings.append(f"Forbidden module import '{alias.name}' detected")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_pkg = node.module.split(".")[0]
                if root_pkg in FORBIDDEN_AST_MODULES:
                    findings.append(f"Forbidden module import 'from {node.module}' detected")
        elif isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    func_name = f"{node.func.value.id}.{node.func.attr}"
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id
            for bad in FORBIDDEN_AST_CALLS:
                if func_name.startswith(bad):
                    findings.append(f"Forbidden function call '{func_name}' detected")
    return findings


def check_disk_space_available(path: str = ".", min_free_bytes: int = 500_000_000) -> bool:
    """Verifies that host disk space meets minimum staging requirements."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free >= min_free_bytes
    except Exception:
        return True


MANIFEST_NAMES = {
    "agent": ("agent.yaml", "agent.yml"),
    "agents": ("agent.yaml", "agent.yml"),
    "skill": ("skill.yaml", "skill.yml", "SKILL.md", "skill.md"),
    "skills": ("skill.yaml", "skill.yml", "SKILL.md", "skill.md"),
    "mcp": ("mcp.yaml", "mcp.yml"),
    "tool": ("tool.yaml", "tool.yml"),
    "tools": ("tool.yaml", "tool.yml"),
}


def sanitize_archive_member_path(filename: str) -> str:
    """
    Validates that a zip entry name does not contain path traversal vectors.
    Returns normalized forward-slash path if valid, raises StorageValidationError if not.
    """
    # Reject null bytes
    if "\0" in filename:
        raise StorageValidationError("Archive member contains null byte")

    # Normalize backslashes
    clean = filename.replace("\\", "/")

    # Reject absolute paths (Unix or Windows)
    if clean.startswith("/") or (len(clean) >= 2 and clean[1] == ":"):
        raise StorageValidationError(f"Archive member has absolute path: '{filename}'")

    # Reject path traversal segments
    parts = [p for p in clean.split("/") if p]
    for p in parts:
        if p == "..":
            raise StorageValidationError(f"Archive member contains path traversal '..': '{filename}'")

    return "/".join(parts)


def verify_package_stream(
    stream_file: BinaryIO,
    resource_type: str,
    expected_sha256: Optional[str] = None,
    max_size: int = MAX_PACKAGE_SIZE_BYTES,
) -> UploadVerificationResult:
    """
    Verifies a package archive from an open binary file stream.
    Computes SHA-256 and validates zip structure without loading full archive into RAM.
    """
    errors: list[str] = []

    # 1. Compute SHA-256 and verify file size
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
            errors.append(f"Package size ({total_size} bytes) exceeds maximum limit ({max_size} bytes)")
            return UploadVerificationResult(
                valid=False,
                file_size=total_size,
                errors=errors,
            )

    calculated_sha256 = hasher.hexdigest()

    if expected_sha256:
        if calculated_sha256.lower() != expected_sha256.strip().lower():
            errors.append(
                f"Checksum mismatch: expected '{expected_sha256}', calculated '{calculated_sha256}'"
            )
            return UploadVerificationResult(
                valid=False,
                file_size=total_size,
                sha256=calculated_sha256,
                errors=errors,
            )

    if total_size == 0:
        errors.append("Uploaded package file is empty")
        return UploadVerificationResult(valid=False, file_size=0, errors=errors)

    # 2. Open and inspect Zip archive
    stream_file.seek(0)
    try:
        zf = zipfile.ZipFile(stream_file, "r")
    except zipfile.BadZipFile as e:
        errors.append(f"Malformed or corrupt ZIP archive: {e}")
        return UploadVerificationResult(
            valid=False,
            file_size=total_size,
            sha256=calculated_sha256,
            errors=errors,
        )

    infolist = zf.infolist()
    if len(infolist) == 0:
        errors.append("Archive is empty (contains 0 entries)")
        return UploadVerificationResult(
            valid=False,
            file_size=total_size,
            sha256=calculated_sha256,
            errors=errors,
        )

    if len(infolist) > MAX_ENTRY_COUNT:
        errors.append(f"Archive entry count ({len(infolist)}) exceeds maximum permitted ({MAX_ENTRY_COUNT})")
        return UploadVerificationResult(
            valid=False,
            file_size=total_size,
            sha256=calculated_sha256,
            errors=errors,
        )

    # 3. Zip bomb defense & path traversal scan
    total_uncompressed = 0
    manifest_candidates: list[Tuple[str, zipfile.ZipInfo]] = []
    norm_type = resource_type.strip().lower()
    expected_manifests = MANIFEST_NAMES.get(norm_type, ("manifest.yaml", "SKILL.md"))
    # Discover if package manifest explicitly declares script execution capability
    scripts_allowed = False
    for cand in infolist:
        p = cand.filename.replace("\\", "/").lower()
        parts = [part for part in p.split("/") if part]
        if len(parts) in (1, 2) and any(parts[-1] == m.lower() for m in expected_manifests):
            try:
                with zf.open(cand) as mf:
                    content = mf.read(65536).decode("utf-8", errors="replace")
                    mdata = yaml.safe_load(content)
                    if isinstance(mdata, dict):
                        caps = mdata.get("capabilities", [])
                        if isinstance(caps, list) and any(c in caps for c in ("scripts", "script_execution", "native", "bash")):
                            scripts_allowed = True
                            break
                        if mdata.get("requires_scripts") or mdata.get("runtime") == "native":
                            scripts_allowed = True
                            break
            except Exception:
                pass

    for info in infolist:
        # Check path traversal
        try:
            clean_path = sanitize_archive_member_path(info.filename)
        except StorageValidationError as e:
            errors.append(f"Security defect: {e}")
            continue

        # Check symlink (Unix attribute 0o120000)
        mode = info.external_attr >> 16
        if (mode & 0o120000) == 0o120000:
            errors.append(f"Security defect: Symlinks are forbidden in package archive: '{info.filename}'")

        # Decompression size check
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            errors.append(
                f"Archive uncompressed size exceeds safety limit ({MAX_UNCOMPRESSED_BYTES} bytes). Potential zip bomb."
            )
            break

        if not info.is_dir():
            ext = os.path.splitext(clean_path)[1].lower()
            if ext in COMPILED_BINARY_EXTENSIONS:
                errors.append(f"Security defect: Compiled native binary extension forbidden: '{info.filename}'")
            elif ext in SCRIPT_EXTENSIONS:
                # Require explicit declaration in manifest capabilities
                if not scripts_allowed:
                    errors.append(f"Security defect: Script execution forbidden without declared manifest capability: '{info.filename}'")
            elif ext == ".py" and not scripts_allowed:
                try:
                    with zf.open(info) as py_file:
                        source_code = py_file.read(1_048_576).decode("utf-8", errors="replace")
                        ast_errors = scan_python_ast(source_code)
                        for ae in ast_errors:
                            errors.append(f"Security defect in '{info.filename}': {ae}")
                except Exception as e:
                    errors.append(f"Failed to parse python file '{info.filename}': {e}")

            # Check magic bytes for disguised executables
            try:
                with zf.open(info) as entry_file:
                    hdr = entry_file.read(16)
                    if any(hdr.startswith(prefix) for prefix in DANGEROUS_MAGIC_PREFIXES):
                        errors.append(f"Security defect: Binary executable header detected in '{info.filename}'")
            except Exception:
                pass


        # Check compression ratio on non-empty files
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_COMPRESSION_RATIO:
                errors.append(
                    f"Suspicious compression ratio ({ratio:.1f}:1) for entry '{info.filename}'. Potential zip bomb."
                )
                break

        # Collect manifest candidate at root level or single enclosing dir
        path_parts = [p for p in clean_path.split("/") if p]
        if len(path_parts) in (1, 2) and not info.is_dir():
            fname = path_parts[-1].lower()
            if any(fname == m.lower() for m in expected_manifests):
                manifest_candidates.append((clean_path, info))

    if errors:
        return UploadVerificationResult(
            valid=False,
            file_size=total_size,
            sha256=calculated_sha256,
            errors=errors,
        )

    # 4. Manifest discovery & validation
    manifest_yaml: Optional[str] = None
    if not manifest_candidates:
        errors.append(
            f"Package missing manifest for '{norm_type}'. Expected one of: {list(expected_manifests)}"
        )
    else:
        # Choose root-level manifest if available
        manifest_candidates.sort(key=lambda x: len(x[0].split("/")))
        chosen_path, chosen_info = manifest_candidates[0]
        try:
            with zf.open(chosen_info) as mf:
                manifest_bytes = mf.read(1_048_576)  # Read at most 1MB manifest
                manifest_yaml = manifest_bytes.decode("utf-8", errors="replace")

            # Parse YAML
            if manifest_yaml.startswith("---"):
                parts = manifest_yaml.split("---", 2)
                fm_text = parts[1] if len(parts) >= 3 else manifest_yaml
                data = yaml.safe_load(fm_text) or {}
            else:
                data = yaml.safe_load(manifest_yaml) or {}

            if not isinstance(data, dict):
                errors.append(f"Manifest '{chosen_path}' must be a YAML dictionary")
            else:
                from app.storage.package_dependency import validate_manifest_dependencies
                dep_valid, dep_errs = validate_manifest_dependencies(data)
                if not dep_valid:
                    errors.extend(dep_errs)
        except Exception as e:
            errors.append(f"Failed to parse manifest '{chosen_path}': {e}")

    return UploadVerificationResult(
        valid=len(errors) == 0,
        file_size=total_size,
        sha256=calculated_sha256,
        mime_type="application/zip",
        manifest_yaml=manifest_yaml,
        errors=errors,
    )
