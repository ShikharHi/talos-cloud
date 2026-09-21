"""
Unit tests for Storage & Marketplace Hardening (Tasks 10–17).
"""

import hashlib
import io
import zipfile
import pytest

from app.storage.package_dependency import (
    CircularDependencyError,
    compute_install_order,
    validate_manifest_dependencies,
    validate_semver_constraint,
)
from app.storage.package_security import (
    DANGEROUS_EXTENSIONS,
    check_disk_space_available,
    verify_package_stream,
)
from app.storage.package_signing import (
    generate_publisher_keypair,
    sign_package_hash,
    verify_package_integrity,
    verify_package_signature,
)


def _make_zip(files: dict[str, str | bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, str):
                zf.writestr(name, content.encode("utf-8"))
            else:
                zf.writestr(name, content)
    buf.seek(0)
    return buf


# ─── Task 11: Upload Security Scanner ─────────────────────────────────────────

def test_task_11_dangerous_extensions_blocked():
    """Verify that dangerous executable extensions (.exe, .dll, .bat, .ps1, etc.) are rejected."""
    for ext in [".exe", ".dll", ".bat", ".ps1", ".wasm"]:
        stream = _make_zip({
            "SKILL.md": "# Valid Skill",
            f"payload{ext}": b"harmless content",
        })
        result = verify_package_stream(stream, resource_type="skill")
        assert result.valid is False
        assert any("forbidden" in err.lower() for err in result.errors)


def test_task_11_magic_bytes_inspection():
    """Verify that disguised binary executables (e.g. .txt containing MZ / ELF header) are rejected."""
    # PE/DOS executable header disguised as data.txt
    stream_pe = _make_zip({
        "SKILL.md": "# Valid Skill",
        "data.txt": b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00",
    })
    result_pe = verify_package_stream(stream_pe, resource_type="skill")
    assert result_pe.valid is False
    assert any("binary executable header" in err.lower() for err in result_pe.errors)

    # ELF header disguised as helper.py
    stream_elf = _make_zip({
        "agent.yaml": "name: test-agent\nkind: agent\n",
        "helper.py": b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    })
    result_elf = verify_package_stream(stream_elf, resource_type="agent")
    assert result_elf.valid is False
    assert any("binary executable header" in err.lower() for err in result_elf.errors)


# ─── Task 14: Package Signing & Integrity Verification ────────────────────────

def test_task_14_ed25519_package_signing():
    """Verify publisher Ed25519 signature generation and tamper detection."""
    priv_hex, pub_hex = generate_publisher_keypair()
    content = b"Mock package zip contents"
    sha256_hex = hashlib.sha256(content).hexdigest()

    # Sign hash
    sig_hex = sign_package_hash(sha256_hex, priv_hex)

    # Valid signature verifies successfully
    assert verify_package_signature(sha256_hex, sig_hex, pub_hex) is True

    # Tampered content fails integrity check
    tampered_content = b"Tampered package zip contents"
    valid, err = verify_package_integrity(
        tampered_content,
        expected_sha256=sha256_hex,
        signature_hex=sig_hex,
        public_key_hex=pub_hex,
    )
    assert valid is False
    assert "tamper detected" in err.lower()

    # Valid content passes integrity check
    valid_ok, err_none = verify_package_integrity(
        content,
        expected_sha256=sha256_hex,
        signature_hex=sig_hex,
        public_key_hex=pub_hex,
    )
    assert valid_ok is True
    assert err_none is None


# ─── Task 16: Package Dependency Resolution ───────────────────────────────────

def test_task_16_semver_constraint_validation():
    """Verify SemVer format constraint parsing."""
    assert validate_semver_constraint("1.0.0") is True
    assert validate_semver_constraint("^2.1.0") is True
    assert validate_semver_constraint("~0.4.2") is True
    assert validate_semver_constraint(">=1.5.0") is True
    assert validate_semver_constraint("invalid_version") is False


def test_task_16_manifest_dependency_validation():
    """Verify parsing of valid and invalid manifest dependency sections."""
    valid_manifest = {
        "name": "my-agent",
        "dependencies": {
            "alice/web-search": "^1.0.0",
            "bob/code-exec": ">=2.0.0",
        },
    }
    valid, errors = validate_manifest_dependencies(valid_manifest)
    assert valid is True
    assert len(errors) == 0

    invalid_manifest = {
        "name": "my-agent",
        "dependencies": {
            "alice/web-search": "not_a_semver",
        },
    }
    invalid, errors = validate_manifest_dependencies(invalid_manifest)
    assert invalid is False
    assert any("invalid semver" in err.lower() for err in errors)


def test_task_16_topological_sort_and_cycle_detection():
    """Verify deterministic install order and circular dependency detection."""
    # Graph: A -> B -> C (A depends on B, B depends on C)
    graph = {
        "pkg_a": ["pkg_b"],
        "pkg_b": ["pkg_c"],
        "pkg_c": [],
    }
    order = compute_install_order(graph)
    # Installation order must install dependency pkg_c first, then pkg_b, then pkg_a
    assert order.index("pkg_c") < order.index("pkg_b") < order.index("pkg_a")

    # Cyclic Graph: X -> Y -> X
    cycle_graph = {
        "pkg_x": ["pkg_y"],
        "pkg_y": ["pkg_x"],
    }
    with pytest.raises(CircularDependencyError) as exc_info:
        compute_install_order(cycle_graph)
    assert "circular dependency detected" in str(exc_info.value).lower()


# ─── Task 10: Concurrency & Disk Space Check ──────────────────────────────────

def test_task_10_disk_space_check():
    """Verify host disk space pre-check helper."""
    assert check_disk_space_available(min_free_bytes=1000) is True
