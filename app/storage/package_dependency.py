"""
Talos Cloud — Package Dependency Resolution & Cycle Detection (Task 16).

Validates package dependencies at publish-time and install-time:
- SemVer constraint format validation.
- Cycle detection (DFS) to prevent cyclic dependency deadlocks.
- Topological sort to compute deterministic installation order.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# SemVer regex supporting exact, ^, ~, >=, <=, >, < ranges
SEMVER_CONSTRAINT_REGEX = re.compile(
    r"^(\^|~|>=|<=|>|<|==)?\s*(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)


def validate_semver_constraint(constraint: str) -> bool:
    """Checks if a string is a valid SemVer version or constraint."""
    if not constraint or not isinstance(constraint, str):
        return False
    return bool(SEMVER_CONSTRAINT_REGEX.match(constraint.strip()))


def validate_manifest_dependencies(manifest_data: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Validates the 'dependencies' section of a parsed package manifest.
    Expected format:
        dependencies:
            "alice/web-search": "^1.0.0"
            "bob/code-sandbox": "~2.1.0"
    """
    errors: list[str] = []
    deps = manifest_data.get("dependencies")
    if deps is None:
        return True, []

    if not isinstance(deps, dict):
        return False, ["'dependencies' field in manifest must be a dictionary of package_name -> version_constraint"]

    for pkg, constraint in deps.items():
        if not isinstance(pkg, str) or not pkg.strip():
            errors.append("Dependency package identifier must be a non-empty string")
        elif not validate_semver_constraint(str(constraint)):
            errors.append(f"Invalid SemVer constraint '{constraint}' for dependency '{pkg}'")

    return len(errors) == 0, errors


class CircularDependencyError(Exception):
    pass


def compute_install_order(dependency_graph: dict[str, list[str]]) -> list[str]:
    """
    Given a mapping of package -> list of dependent packages,
    computes deterministic installation order using topological sort (Kahn's or DFS).
    Raises CircularDependencyError if a cycle is detected.
    """
    # 0 = unvisited, 1 = visiting (in current call stack), 2 = visited
    state: dict[str, int] = {node: 0 for node in dependency_graph}
    order: list[str] = []

    def dfs(node: str, stack: list[str]):
        state[node] = 1
        stack.append(node)
        for dep in dependency_graph.get(node, []):
            if dep not in state:
                state[dep] = 0
            if state[dep] == 1:
                cycle_path = " -> ".join(stack + [dep])
                raise CircularDependencyError(f"Circular dependency detected: {cycle_path}")
            if state[dep] == 0:
                dfs(dep, stack)
        state[node] = 2
        stack.pop()
        order.append(node)

    for node in list(dependency_graph.keys()):
        if state[node] == 0:
            dfs(node, [])

    return order
