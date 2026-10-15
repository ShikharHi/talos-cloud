import os
import re
import yaml
import shutil
import zipfile
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Request, Query, Header, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from auth.repository import auth_repo
from marketplace.repository import marketplace_repo
from config import config

router = APIRouter(prefix="/v1/marketplace", tags=["Marketplace"])

# Persistent storage directories
STORAGE_SKILLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage", "skills"))
USER_HOME = os.path.expanduser("~")
DOT_TALOS_SKILLS_DIR = os.path.join(USER_HOME, ".talos", "skills")

os.makedirs(STORAGE_SKILLS_DIR, exist_ok=True)
os.makedirs(DOT_TALOS_SKILLS_DIR, exist_ok=True)

def unpack_skill_zip(zip_file_path: str, dest_dir: str):
    """Safely extracts a skill zip into dest_dir, flattening top-level container folders if present."""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_file_path, 'r') as zf:
        members = [m for m in zf.namelist() if m and not m.startswith('__MACOSX/')]
        non_empty = [m for m in members if not m.endswith('/')]

        # Check if entire zip is wrapped in a single root folder (e.g. ppt/SKILL.md)
        prefix = ''
        if non_empty:
            first_parts = [m.split('/')[0] for m in non_empty if '/' in m]
            if len(first_parts) == len(non_empty) and len(set(first_parts)) == 1:
                prefix = first_parts[0] + '/'

        for member in zf.infolist():
            filename = member.filename
            if not filename or filename.startswith('__MACOSX/') or filename.endswith('.DS_Store'):
                continue

            rel_path = filename[len(prefix):] if prefix and filename.startswith(prefix) else filename
            if not rel_path or ".." in rel_path or rel_path.startswith("/") or rel_path.startswith("\\"):
                continue

            target_path = os.path.join(dest_dir, rel_path.replace('/', os.sep))
            if member.is_dir():
                os.makedirs(target_path, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with zf.open(member) as source, open(target_path, 'wb') as target:
                    shutil.copyfileobj(source, target)

async def get_optional_user(request: Request) -> Optional[Dict[str, Any]]:
    session_id = request.cookies.get("talos_session")
    if session_id:
        session = await auth_repo.get_valid_session(session_id)
        if session:
            user = await auth_repo.get_user_by_id(session["user_id"])
            if user:
                return user

    # Fallback to local default user in PostgreSQL
    with auth_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email, display_name FROM users ORDER BY created_at ASC LIMIT 1")
            row = cur.fetchone()
            if row:
                return dict(row)
            # Create usr_local_dev if users table is completely empty
            cur.execute("""
                INSERT INTO users (id, email, password_hash, display_name)
                VALUES ('usr_local_dev', 'shikhar@talos.dev', 'dummy_hash', 'Shikhar')
                ON CONFLICT (id) DO NOTHING
            """)
            conn.commit()
            return {"id": "usr_local_dev", "email": "shikhar@talos.dev", "display_name": "Shikhar"}

async def get_current_user_required(request: Request) -> Dict[str, Any]:
    user = await get_optional_user(request)
    if not user:
        return {"id": "usr_local_dev", "email": "shikhar@talos.dev", "display_name": "Shikhar"}
    return user

class PublishSkillRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=60)
    namespace: str = Field(..., pattern=r"^@[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-]+$")
    description: str = Field(..., min_length=10, max_length=300)
    tags: List[str] = Field(default_factory=list)
    icon: str = Field(default="bot")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    manifest_md: str = Field(..., min_length=20)

@router.get("/skills")
async def list_marketplace_skills(
    query: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    sort: str = Query("popular"),
    request: Request = None
):
    """List skills with full-text search, category tags, and install status."""
    current_user = await get_optional_user(request)
    user_id = current_user["id"] if current_user else "usr_local_dev"
    skills = await marketplace_repo.list_skills(query=query, tag=tag, sort_by=sort, current_user_id=user_id)
    return {"skills": skills, "count": len(skills)}

@router.get("/skills/{namespace_or_id:path}/download")
async def download_skill_package(namespace_or_id: str):
    """Download the persistent .zip package for a skill from storage."""
    skill = await marketplace_repo.get_skill_by_id(namespace_or_id)
    if not skill:
        skill = await marketplace_repo.get_skill(namespace_or_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    zip_path = skill.get("zip_path") or skill.get("version_zip_path")
    if zip_path and zip_path.startswith("tigris://"):
        from fastapi.responses import RedirectResponse
        from marketplace.tigris_service import tigris_service
        download_url = tigris_service.generate_download_url(zip_path, expires_in=3600)
        return RedirectResponse(url=download_url)

    if not zip_path or not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Package zip file not found in storage")

    filename = os.path.basename(zip_path)
    return FileResponse(zip_path, media_type="application/zip", filename=filename)

@router.get("/skills/{namespace:path}")
async def get_skill_details(namespace: str, request: Request = None):
    """Fetch complete skill manifest and version history."""
    current_user = await get_optional_user(request)
    user_id = current_user["id"] if current_user else "usr_local_dev"
    skill = await marketplace_repo.get_skill(namespace, current_user_id=user_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill

@router.get("/user/installed")
async def get_user_installed(current_user: Dict[str, Any] = Depends(get_current_user_required)):
    """List skills installed by the current user."""
    skills = await marketplace_repo.get_user_installed_skills(current_user["id"])
    return {"installed": skills}

@router.get("/user/published")
async def get_user_published(current_user: Dict[str, Any] = Depends(get_current_user_required)):
    """List skills authored by the current user."""
    skills = await marketplace_repo.get_user_published_skills(current_user["id"])
    return {"published": skills}

@router.post("/skills/{skill_id}/install")
async def install_skill(skill_id: str, current_user: Dict[str, Any] = Depends(get_current_user_required)):
    """
    Install skill into user profile and unpack its zip files onto user's local PC.
    """
    user_id = current_user["id"]
    await marketplace_repo.install_skill(user_id, skill_id)

    # 1. Fetch skill record from database
    skill = await marketplace_repo.get_skill_by_id(skill_id)
    if not skill:
        skill = await marketplace_repo.get_skill(skill_id)

    installed_path = None
    if skill:
        slug = skill.get("namespace", "").split("/")[-1] if "/" in skill.get("namespace", "") else skill["name"].lower().replace(" ", "-")
        slug = re.sub(r'[^a-zA-Z0-9_\-]', '', slug) or "custom-skill"
        
        target_dir = os.path.join(DOT_TALOS_SKILLS_DIR, slug)
        zip_path = skill.get("zip_path") or skill.get("version_zip_path")

        os.makedirs(target_dir, exist_ok=True)
        if zip_path and os.path.exists(zip_path):
            unpack_skill_zip(zip_path, target_dir)
        elif skill.get("manifest_md"):
            with open(os.path.join(target_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(skill["manifest_md"])
        installed_path = target_dir

    return {
        "success": True,
        "message": "Skill installed successfully into ~/.talos/skills/",
        "installed_path": installed_path
    }

@router.post("/skills/{skill_id}/uninstall")
async def uninstall_skill(skill_id: str, current_user: Dict[str, Any] = Depends(get_current_user_required)):
    """Uninstall skill from user profile and remove local extension folder."""
    user_id = current_user["id"]
    success = await marketplace_repo.uninstall_skill(user_id, skill_id)

    skill = await marketplace_repo.get_skill_by_id(skill_id)
    if not skill:
        skill = await marketplace_repo.get_skill(skill_id)

    if skill:
        slug = skill.get("namespace", "").split("/")[-1] if "/" in skill.get("namespace", "") else skill["name"].lower().replace(" ", "-")
        slug = re.sub(r'[^a-zA-Z0-9_\-]', '', slug)
        target_dir = os.path.join(DOT_TALOS_SKILLS_DIR, slug)
        if os.path.exists(target_dir):
            try:
                shutil.rmtree(target_dir)
            except Exception:
                pass

    return {"success": success, "message": "Skill uninstalled and local extension files removed"}

@router.post("/skills/{skill_id}/star")
async def toggle_star_skill(skill_id: str, current_user: Dict[str, Any] = Depends(get_current_user_required)):
    """Toggle star on a skill."""
    result = await marketplace_repo.toggle_star(current_user["id"], skill_id)
    return result

@router.delete("/skills/{skill_id}")
@router.post("/skills/{skill_id}/unpublish")
async def unpublish_skill(skill_id: str, current_user: Dict[str, Any] = Depends(get_current_user_required)):
    """
    Unpublish a skill from the marketplace.
    Enforces that only the author/publisher of that skill can unpublish it.
    Deletes the skill package from persistent storage and local disk.
    """
    user_id = current_user["id"]
    try:
        deleted = await marketplace_repo.delete_skill(skill_id, user_id)
    except ValueError as val_err:
        raise HTTPException(status_code=403, detail=str(val_err))

    if not deleted:
        raise HTTPException(status_code=404, detail="Skill not found or already deleted")

    # 1. Delete .zip file from persistent storage/skills
    zip_path = deleted.get("zip_path")
    if zip_path and os.path.exists(zip_path):
        try:
            os.remove(zip_path)
        except Exception as e:
            print(f"[Marketplace] Error deleting storage zip: {e}")

    # Also clean up by slug name in storage dir if exists
    slug = deleted.get("namespace", "").split("/")[-1] if "/" in deleted.get("namespace", "") else deleted["name"].lower().replace(" ", "-")
    slug = re.sub(r'[^a-zA-Z0-9_\-]', '', slug)
    if slug:
        named_zip = os.path.join(STORAGE_SKILLS_DIR, f"{slug}.zip")
        if os.path.exists(named_zip):
            try:
                os.remove(named_zip)
            except Exception:
                pass

        # 2. Delete local unpacked skill directory if publisher also had it installed
        target_dir = os.path.join(DOT_TALOS_SKILLS_DIR, slug)
        if os.path.exists(target_dir):
            try:
                shutil.rmtree(target_dir)
            except Exception:
                pass

    return {
        "success": True,
        "message": f"Skill '{deleted.get('name')}' has been unpublished and permanently deleted from storage."
    }

@router.post("/publish-package")
async def publish_skill_package(
    name: str = Form(...),
    file: UploadFile = File(...),
    description: Optional[str] = Form(None),
    tags: Optional[str] = Form(""),
    category: Optional[str] = Form("Custom"),
    icon: Optional[str] = Form("bot"),
    version: Optional[str] = Form("1.0.0"),
    current_user: Dict[str, Any] = Depends(get_current_user_required)
):
    """
    Publish a zipped skill package into persistent storage and index in PostgreSQL.
    Extracts SKILL.md if present, or synthesizes one if missing.
    Auto-installs it directly onto the user's PC.
    """
    if not name or len(name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Skill name must be at least 2 characters")

    # Generate clean slug & namespace
    raw_slug = re.sub(r'[^a-zA-Z0-9_\-]', '-', name.lower()).strip('-')
    slug = re.sub(r'-+', '-', raw_slug) or "custom-skill"

    author_handle = current_user.get("email", "").split("@")[0] or "custom"
    author_handle = re.sub(r'[^a-zA-Z0-9_\-]', '', author_handle.lower()) or "user"
    namespace = f"@{author_handle}/{slug}"

    # 1. Save uploaded zip file to persistent storage directory
    target_zip_filename = f"{slug}.zip"
    target_zip_path = os.path.join(STORAGE_SKILLS_DIR, target_zip_filename)

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file cannot be empty")

    with open(target_zip_path, "wb") as f:
        f.write(file_bytes)

    # 2. Inspect ZIP package & read or generate SKILL.md with comprehensive validation
    manifest_md = None
    frontmatter = {}
    is_valid_zip = zipfile.is_zipfile(target_zip_path)

    if not is_valid_zip:
        if os.path.exists(target_zip_path):
            os.remove(target_zip_path)
        raise HTTPException(status_code=400, detail="Invalid package: The uploaded file is not a valid ZIP archive.")

    try:
        with zipfile.ZipFile(target_zip_path, 'r') as zf:
            namelist = zf.namelist()
            if not namelist:
                if os.path.exists(target_zip_path):
                    os.remove(target_zip_path)
                raise HTTPException(status_code=400, detail="Invalid package: The ZIP file is completely empty.")

            # Security: check against Zip Slip / path traversal
            for name_entry in namelist:
                if ".." in name_entry or name_entry.startswith("/") or name_entry.startswith("\\"):
                    if os.path.exists(target_zip_path):
                        os.remove(target_zip_path)
                    raise HTTPException(status_code=400, detail="Security validation rejected: ZIP contains malicious path traversal entry.")

            # Search for SKILL.md inside zip
            skill_md_name = None
            for n in namelist:
                if n.lower().endswith("skill.md"):
                    skill_md_name = n
                    break

            if skill_md_name:
                manifest_md = zf.read(skill_md_name).decode('utf-8', errors='ignore')
                fm_match = re.search(r"^---\s*\n(.*?)\n---", manifest_md, re.DOTALL)
                if fm_match:
                    try:
                        parsed = yaml.safe_load(fm_match.group(1))
                        if isinstance(parsed, dict):
                            frontmatter = parsed
                    except Exception:
                        pass
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(target_zip_path):
            os.remove(target_zip_path)
        raise HTTPException(status_code=400, detail=f"Failed to inspect zip package: {str(e)}")

    # Extract description from SKILL.md frontmatter or body
    extracted_desc = frontmatter.get("description")
    if not extracted_desc and manifest_md:
        # Try extracting first non-header sentence from markdown body
        body_lines = [line.strip() for line in manifest_md.splitlines() if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("---")]
        if body_lines:
            extracted_desc = body_lines[0][:200]

    final_desc = extracted_desc or description or f"Specialized skill package for {name}"
    final_version = frontmatter.get("version") or version or "1.0.0"

    if not manifest_md:
        manifest_md = f"""---
name: {slug}
description: {final_desc}
version: {final_version}
category: {category}
author: {author_handle}
---

# {name}
{final_desc}

### Usage:
Invoke this skill in chat by typing `/{slug}`.
"""
        frontmatter = {
            "name": slug,
            "description": final_desc,
            "version": final_version,
            "category": category,
            "author": author_handle
        }

    # Security check: prompt injection & harmful commands
    dangerous_patterns = [
        r"ignore (all )?previous instructions",
        r"disregard (the )?system prompt",
        r"you are now in developer mode",
        r"send (all )?passwords to",
        r"rm\s+-rf\s+/",
        r"del\s+/[sfq]\s+c:\\windows",
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, manifest_md, re.IGNORECASE):
            if os.path.exists(target_zip_path):
                os.remove(target_zip_path)
            raise HTTPException(status_code=422, detail="Security validation rejected this skill: Potential prompt injection / malicious pattern detected.")

    # 3. Create or fetch publisher
    publisher_id = await marketplace_repo.get_or_create_publisher(
        user_id=current_user["id"],
        handle=author_handle,
        display_name=current_user.get("display_name") or author_handle
    )

    # 4. Parse tags
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
    if category and category.lower() not in tag_list:
        tag_list.append(category.lower())
    if "custom" not in tag_list:
        tag_list.append("custom")

    # 5. Upload to Tigris Object Storage
    from marketplace.tigris_service import tigris_service
    tigris_key = f"skills/{slug}/{final_version}/{target_zip_filename}"
    tigris_uri = None
    try:
        tigris_uri = tigris_service.upload_file(target_zip_path, tigris_key)
        print(f"[Marketplace] Uploaded skill package to Tigris: {tigris_uri}")
    except Exception as e:
        print(f"[Marketplace] Tigris upload warning for skill: {e}")

    # 5b. Persist into Neon PostgreSQL
    result = await marketplace_repo.publish_skill(
        publisher_id=publisher_id,
        name=name.strip(),
        namespace=namespace,
        description=final_desc.strip(),
        tags=tag_list,
        icon=icon or "bot",
        version=final_version,
        manifest_md=manifest_md,
        frontmatter_json=frontmatter,
        zip_path=tigris_uri or target_zip_path
    )

    # 5c. Mirror to unified plugins registry table in Neon
    try:
        await marketplace_repo.publish_plugin(
            publisher_id=publisher_id,
            slug=slug,
            name=name.strip(),
            description=final_desc.strip(),
            plugin_type="skill",
            version=final_version,
            icon_url=icon or "bot",
            zip_path=tigris_uri or target_zip_path,
            manifest=frontmatter
        )
    except Exception as e:
        print(f"Warning: could not sync skill to unified plugins table: {e}")

    return {
        "success": True,
        "skill": {
            **result,
            "name": name.strip(),
            "description": final_desc.strip(),
            "tags": tag_list,
            "slug": slug,
            "installed_path": None,
            "storage_path": target_zip_path
        }
    }

@router.post("/publish")
async def publish_skill(payload: PublishSkillRequest, current_user: Dict[str, Any] = Depends(get_current_user_required)):
    """
    Publish or update a skill via JSON manifest.
    """
    fm_match = re.search(r"^---\s*\n(.*?)\n---", payload.manifest_md, re.DOTALL)
    if not fm_match:
        raise HTTPException(status_code=400, detail="Invalid SKILL.md: Must contain YAML frontmatter enclosed in '---'")
    
    try:
        frontmatter = yaml.safe_load(fm_match.group(1))
        if not isinstance(frontmatter, dict):
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid YAML syntax in SKILL.md frontmatter")

    dangerous_patterns = [
        r"ignore (all )?previous instructions",
        r"disregard (the )?system prompt",
        r"you are now in developer mode",
        r"send (all )?passwords to",
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, payload.manifest_md, re.IGNORECASE):
            raise HTTPException(status_code=422, detail="Security validation rejected this skill: Potential prompt injection / jailbreak detected.")

    author_handle = current_user.get("email", "").split("@")[0] or "user"
    publisher_id = await marketplace_repo.get_or_create_publisher(
        user_id=current_user["id"],
        handle=author_handle,
        display_name=current_user.get("display_name") or author_handle
    )

    slug = payload.namespace.split("/")[-1]
    extract_dir = os.path.join(DOT_TALOS_SKILLS_DIR, slug)
    os.makedirs(extract_dir, exist_ok=True)
    with open(os.path.join(extract_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(payload.manifest_md)

    try:
        result = await marketplace_repo.publish_skill(
            publisher_id=publisher_id,
            name=payload.name,
            namespace=payload.namespace,
            description=payload.description,
            tags=[t.strip().lower() for t in payload.tags if t.strip()],
            icon=payload.icon,
            version=payload.version,
            manifest_md=payload.manifest_md,
            frontmatter_json=frontmatter
        )
        await marketplace_repo.install_skill(current_user["id"], result["id"])
        return {"success": True, "skill": result}
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

@router.get("/internal/user/{user_id}/active-skills")
async def get_active_skills_for_agent(
    user_id: str,
    x_talos_internal_token: Optional[str] = Header(None)
):
    """
    Internal route for talos-backend LangGraph ReAct agent.
    Returns active installed skills with frontmatter and instructions.
    """
    if x_talos_internal_token != config.TALOS_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden: internal agent access only")
    
    skills = await marketplace_repo.get_user_installed_skills(user_id)
    return {"skills": skills}
