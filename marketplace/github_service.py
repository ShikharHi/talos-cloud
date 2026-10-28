"""
Talos GitHub Skill Importer & Packager.

Provides repository inspection, monorepo multi-skill discovery,
and targeted subfolder slicing/packaging into standard Talos/Agent skill ZIPs
without requiring full local git clones (similar to npx skills add and giget).
"""
import asyncio
import io
import os
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import httpx
import yaml


def parse_github_url(raw_url: str) -> Dict[str, Any]:
    """
    Parse a GitHub URL or shorthand into its components:
    - owner/repo
    - https://github.com/owner/repo
    - https://github.com/owner/repo/tree/branch/subpath
    - https://github.com/owner/repo/blob/branch/subpath/SKILL.md
    - https://raw.githubusercontent.com/owner/repo/branch/subpath/SKILL.md
    """
    url = raw_url.strip().rstrip("/")

    # Remove trailing .git or slashes
    if url.endswith(".git"):
        url = url[:-4]
    url = url.rstrip("/")

    # Raw githubusercontent
    raw_match = re.match(r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)(?:/(.*))?$", url, re.I)
    if raw_match:
        owner, repo, branch, fullpath = raw_match.groups()
        subpath = fullpath or ""
        if subpath.lower().endswith("skill.md"):
            subpath = os.path.dirname(subpath)
        return {"owner": owner, "repo": repo, "branch": branch, "subpath": subpath.strip("/")}

    # Standard github.com URL
    gh_match = re.match(r"^https?://github\.com/([^/]+)/([^/]+)(?:/(tree|blob)/([^/]+)(?:/(.*))?)?$", url, re.I)
    if gh_match:
        owner, repo, mode, branch, fullpath = gh_match.groups()
        subpath = fullpath or ""
        if subpath.lower().endswith("skill.md"):
            subpath = os.path.dirname(subpath)
        return {"owner": owner, "repo": repo, "branch": branch or None, "subpath": subpath.strip("/")}

    # Shorthand: owner/repo or owner/repo/path/to/subfolder
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 2 and not url.startswith("http"):
        owner = parts[0]
        repo = parts[1]
        subpath = "/".join(parts[2:]) if len(parts) > 2 else ""
        if subpath.lower().endswith("skill.md"):
            subpath = os.path.dirname(subpath)
        return {"owner": owner, "repo": repo, "branch": None, "subpath": subpath.strip("/")}

    raise ValueError(f"Invalid GitHub URL or repository shorthand: '{raw_url}'")


async def resolve_default_branch(owner: str, repo: str, client: httpx.AsyncClient) -> str:
    """Fetch default branch for repository."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    resp = await client.get(url)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("default_branch") or "main"
    elif resp.status_code == 404:
        raise ValueError(f"GitHub repository '{owner}/{repo}' not found or is private.")
    elif resp.status_code == 403:
        # Rate limit hit: fallback to testing 'main' then 'master'
        return "main"
    return "main"


def parse_frontmatter(content: str) -> Dict[str, Any]:
    """Parse YAML frontmatter from markdown content."""
    meta = {}
    if not content:
        return meta

    content = content.lstrip()
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            raw_yaml = parts[1]
            try:
                parsed = yaml.safe_load(raw_yaml)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                pass
    return meta


async def inspect_github_skills(
    raw_url: str,
    github_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Inspect a GitHub repository and discover all skills inside it.
    Supports monorepos with multiple skills in subfolders.
    """
    parsed = parse_github_url(raw_url)
    owner = parsed["owner"]
    repo = parsed["repo"]
    specified_branch = parsed["branch"]
    target_subpath = parsed["subpath"]

    headers = {
        "User-Agent": "Talos-Agent-Skill-Importer/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    async with httpx.AsyncClient(headers=headers, timeout=30.0, follow_redirects=True) as client:
        branch = specified_branch or await resolve_default_branch(owner, repo, client)

        # 1. Fetch recursive git tree
        tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        resp = await client.get(tree_url)

        skill_files = []
        if resp.status_code == 200:
            tree_data = resp.json()
            for item in tree_data.get("tree", []):
                if item.get("type") == "blob":
                    path = item.get("path", "")
                    if path.lower().endswith("skill.md"):
                        skill_files.append(path)
        else:
            # Fallback: if tree API fails (e.g. rate limit or massive repo), try fetching common skill paths directly
            candidates = [
                f"{target_subpath}/SKILL.md".strip("/") if target_subpath else "SKILL.md",
                "SKILL.md",
                "skills/SKILL.md",
            ]
            for cand in candidates:
                cand_clean = cand.lstrip("/")
                raw_url_cand = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{cand_clean}"
                cand_resp = await client.get(raw_url_cand)
                if cand_resp.status_code == 200:
                    skill_files.append(cand_clean)

        # Filter by target_subpath if user specified a specific folder
        if target_subpath:
            filtered = [
                p for p in skill_files
                if p == target_subpath or p == f"{target_subpath}/SKILL.md" or p.startswith(f"{target_subpath}/")
            ]
            if filtered:
                skill_files = filtered

        # If still no SKILL.md found, check if target_subpath itself has a README or is a folder
        if not skill_files and target_subpath:
            skill_files.append(f"{target_subpath}/SKILL.md")

        # 2. Extract metadata for each discovered skill concurrently
        sem = asyncio.Semaphore(20)

        async def fetch_meta(s_path: str) -> Dict[str, Any]:
            async with sem:
                # Normalize folder path to forward slashes
                folder_path = str(Path(s_path).parent).replace("\\", "/")
                if folder_path == ".":
                    folder_path = ""
                raw_url_file = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{s_path}"
                try:
                    f_resp = await client.get(raw_url_file, timeout=12.0)
                    content = f_resp.text if f_resp.status_code == 200 else ""
                except Exception:
                    content = ""
                meta = parse_frontmatter(content)

                folder_basename = Path(folder_path).name if folder_path else repo
                skill_name = meta.get("name") or folder_basename.replace("-", " ").replace("_", " ").title()
                skill_slug = re.sub(r"[^a-zA-Z0-9_\-]", "-", (meta.get("name") or folder_basename).lower()).strip("-")
                skill_desc = meta.get("description") or ""

                if not skill_desc and content:
                    for line in content.split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#") and not line.startswith("---") and not line.startswith("```"):
                            skill_desc = line[:180]
                            break

                tags = meta.get("tags") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]

                return {
                    "name": skill_name,
                    "slug": skill_slug,
                    "description": skill_desc or f"Skill workflow from {owner}/{repo}",
                    "path": folder_path,
                    "skill_md_path": s_path,
                    "version": str(meta.get("version") or "1.0.0"),
                    "tags": tags,
                }

        tasks = [fetch_meta(s) for s in skill_files[:50]]
        discovered_skills = await asyncio.gather(*tasks)

        return {
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "url": f"https://github.com/{owner}/{repo}",
            "count": len(discovered_skills),
            "skills": discovered_skills,
        }


async def package_github_skill(
    owner: str,
    repo: str,
    branch: str,
    subpath: str,
    github_token: Optional[str] = None
) -> Tuple[bytes, str]:
    """
    Download repository archive from GitHub, extract the target subfolder,
    and package it into a clean standard ZIP archive with SKILL.md at the root.
    Returns (zip_bytes, suggested_slug).
    """
    subpath = subpath.strip("/")
    headers = {
        "User-Agent": "Talos-Agent-Skill-Importer/1.0",
    }
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    tarball_url = f"https://api.github.com/repos/{owner}/{repo}/tarball/{branch}"

    async with httpx.AsyncClient(headers=headers, timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(tarball_url)
        if resp.status_code != 200:
            # Fallback to direct codeload zip
            zip_fallback_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
            resp = await client.get(zip_fallback_url)
            if resp.status_code != 200:
                raise ValueError(f"Failed to download repository archive from GitHub: HTTP {resp.status_code}")

            # Extract from ZIP
            in_zip = zipfile.ZipFile(io.BytesIO(resp.content))
            names = in_zip.namelist()
            root_prefix = names[0].split("/")[0] if names else ""
            target_prefix = f"{root_prefix}/{subpath}/".rstrip("/") + "/" if subpath else f"{root_prefix}/"

            out_buf = io.BytesIO()
            with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out_zip:
                for name in names:
                    if name.startswith(target_prefix) and not name.endswith("/"):
                        rel_path = name[len(target_prefix):]
                        if rel_path:
                            out_zip.writestr(rel_path, in_zip.read(name))

            suggested_slug = os.path.basename(subpath) if subpath else repo
            return out_buf.getvalue(), suggested_slug

        # Extract from TARBALL (gz)
        tar = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz")
        members = tar.getmembers()

        root_prefix = members[0].name.split("/")[0] if members else ""
        target_prefix = f"{root_prefix}/{subpath}/".rstrip("/") + "/" if subpath else f"{root_prefix}/"

        matching_members = [
            m for m in members
            if m.isfile() and m.name.startswith(target_prefix)
        ]

        if not matching_members and subpath:
            # Try looser match if branch folder differed
            matching_members = [
                m for m in members
                if m.isfile() and (f"/{subpath}/" in m.name or m.name.endswith(f"/{subpath}"))
            ]
            if matching_members:
                first_name = matching_members[0].name
                target_prefix = first_name.split(subpath)[0] + subpath + "/"

        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out_zip:
            for m in matching_members:
                f = tar.extractfile(m)
                if f:
                    rel_path = m.name[len(target_prefix):]
                    if rel_path:
                        out_zip.writestr(rel_path, f.read())

        suggested_slug = os.path.basename(subpath) if subpath else repo
        return out_buf.getvalue(), suggested_slug
