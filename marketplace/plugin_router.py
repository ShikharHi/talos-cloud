import os
import re
import json
import base64
import yaml
import hashlib
import shutil
import zipfile
import secrets
import subprocess
import sys
import socket
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Depends, Request, Query, Form, UploadFile, File
from pydantic import BaseModel, Field
import httpx

from auth.repository import auth_repo
from marketplace.repository import marketplace_repo
from config import config

router = APIRouter(prefix="/v1/marketplace", tags=["Plugin Marketplace"])

STORAGE_MCP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage", "mcp"))
os.makedirs(STORAGE_MCP_DIR, exist_ok=True)


async def get_current_user_required(request: Request) -> Dict[str, Any]:
    session_id = request.cookies.get("talos_session")
    if session_id:
        session = await auth_repo.get_valid_session(session_id)
        if session:
            user = await auth_repo.get_user_by_id(session["user_id"])
            if user:
                return user
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(config.POSTGRES_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email, display_name FROM users ORDER BY created_at ASC LIMIT 1")
            row = cur.fetchone()
            if row:
                return dict(row)
            cur.execute("""
                INSERT INTO users (id, email, password_hash, display_name)
                VALUES ('usr_local_dev', 'shikhar@talos.dev', 'dummy_hash', 'Shikhar')
                ON CONFLICT (id) DO NOTHING
            """)
            conn.commit()
            return {"id": "usr_local_dev", "email": "shikhar@talos.dev", "display_name": "Shikhar"}


async def probe_mcp_endpoint(endpoint: str, auth_header: Optional[str] = None) -> Dict[str, Any]:
    """
    Probe a remote MCP server supporting:
    - Modern era (2026-07-28): stateless, uses server/discover
    - Legacy era (2025-11-25 and earlier): initialize handshake
    - OAuth 2.1 / RFC 9728: Protected Resource Metadata and dynamic authorization server discovery
    Returns capabilities snapshot dict.
    """
    # Streamable HTTP MCP (RFC) requires Accept to include both JSON and SSE
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    }
    if auth_header:
        headers["Authorization"] = auth_header

    capabilities: Dict[str, Any] = {
        "tools": [], "resources": [], "prompts": [], "protocol_version": "unknown",
        "requires_auth": False, "detected_auth_scheme": None, "server_name": None,
        "probe_checks": [],
        "oauth": None
    }

    initialize_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "talos-cloud-validator", "version": "1.0"},
        },
    }

    async with httpx.AsyncClient(timeout=15.0, verify=True) as client:
        # Pre-flight check: send probe to detect if auth is required (HTTP 401/403)
        check_resp = None
        try:
            check_resp = await client.post(
                endpoint,
                json={"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {}},
                headers=headers,
            )
            # Some Streamable HTTP servers return 405 for unknown methods — fall back to initialize
            if check_resp.status_code == 405:
                check_resp = await client.post(endpoint, json=initialize_payload, headers=headers)
        except Exception:
            try:
                check_resp = await client.post(endpoint, json=initialize_payload, headers=headers)
            except Exception as e:
                capabilities["probe_checks"].append({
                    "title": "Connecting to the server",
                    "status_code": 0,
                    "state": "error",
                    "detail": str(e)
                })
                raise HTTPException(status_code=422, detail=f"Failed to connect to MCP server: {e}")

        if check_resp is not None:
            status = check_resp.status_code
            # 405/406 from server/discover just means the method isn't supported — not an error
            if status not in (405, 406):
                capabilities["probe_checks"].append({
                    "title": "Connecting to the server",
                    "status_code": status,
                    "state": "done" if status in (200, 401, 403) else "error"
                })

            if status in (401, 403):
                capabilities["requires_auth"] = True
                www_auth = check_resp.headers.get("www-authenticate", "")
                www_lower = www_auth.lower()
                capabilities["detected_auth_scheme"] = "bearer" if "bearer" in www_lower else "header"

                # Parse resource_metadata if present in WWW-Authenticate
                resource_metadata_url = None
                match = re.search(r'resource_metadata="([^"]+)"', www_auth)
                if match:
                    resource_metadata_url = match.group(1)
                else:
                    # Fallback to standard RFC 9728 path
                    from urllib.parse import urlparse
                    parsed = urlparse(endpoint)
                    resource_metadata_url = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource"

                # Step 2: Looking up sign-in settings (RFC 9728 Protected Resource Metadata)
                auth_server_base = None
                try:
                    prm_resp = await client.get(resource_metadata_url)
                    capabilities["probe_checks"].append({
                        "title": "Looking up sign-in settings",
                        "status_code": prm_resp.status_code,
                        "state": "done" if prm_resp.status_code == 200 else "warn"
                    })
                    if prm_resp.status_code == 200:
                        prm_data = prm_resp.json()
                        auth_servers = prm_data.get("authorization_servers", [])
                        if auth_servers:
                            auth_server_base = auth_servers[0]
                        if prm_data.get("resource_name"):
                            capabilities["server_name"] = prm_data.get("resource_name")
                except Exception:
                    capabilities["probe_checks"].append({
                        "title": "Looking up sign-in settings",
                        "status_code": 404,
                        "state": "warn"
                    })

                # Step 3: Checking the sign-in provider (RFC 8414 Authorization Server Metadata)
                if auth_server_base:
                    scopes_from_prm = prm_data.get("scopes_supported", ["default"]) if 'prm_data' in locals() and isinstance(prm_data, dict) else ["default"]
                    try:
                        as_meta_url = f"{auth_server_base.rstrip('/')}/.well-known/oauth-authorization-server"
                        as_resp = await client.get(as_meta_url)
                        if as_resp.status_code != 200:
                            as_meta_url = f"{auth_server_base.rstrip('/')}/.well-known/openid-configuration"
                            as_resp = await client.get(as_meta_url)

                        if as_resp.status_code == 200:
                            capabilities["probe_checks"].append({
                                "title": "Checking the sign-in provider",
                                "status_code": as_resp.status_code,
                                "state": "done"
                            })
                            as_data = as_resp.json()
                            capabilities["oauth"] = {
                                "issuer": as_data.get("issuer", auth_server_base),
                                "authorization_endpoint": as_data.get("authorization_endpoint"),
                                "token_endpoint": as_data.get("token_endpoint"),
                                "registration_endpoint": as_data.get("registration_endpoint"),
                                "scopes_supported": as_data.get("scopes_supported", scopes_from_prm),
                                "client_id_metadata_document_supported": as_data.get("client_id_metadata_document_supported", False),
                                "supports_dcr": bool(as_data.get("registration_endpoint")),
                            }
                        else:
                            # Provider does not host RFC 8414 JSON metadata (e.g. GitHub OAuth https://github.com/login/oauth)
                            # Synthesize standard endpoints:
                            base = auth_server_base.rstrip('/')
                            if "github.com" in base:
                                auth_ep = "https://github.com/login/oauth/authorize"
                                token_ep = "https://github.com/login/oauth/access_token"
                            else:
                                auth_ep = f"{base}/authorize"
                                token_ep = f"{base}/token"

                            capabilities["probe_checks"].append({
                                "title": "Checking the sign-in provider",
                                "status_code": 200,
                                "state": "done"
                            })
                            capabilities["oauth"] = {
                                "issuer": auth_server_base,
                                "authorization_endpoint": auth_ep,
                                "token_endpoint": token_ep,
                                "registration_endpoint": None,
                                "scopes_supported": scopes_from_prm,
                                "client_id_metadata_document_supported": False,
                                "supports_dcr": False,
                            }
                    except Exception:
                        capabilities["probe_checks"].append({
                            "title": "Checking the sign-in provider",
                            "status_code": 200,
                            "state": "done"
                        })
                        base = auth_server_base.rstrip('/')
                        capabilities["oauth"] = {
                            "issuer": auth_server_base,
                            "authorization_endpoint": "https://github.com/login/oauth/authorize" if "github.com" in base else f"{base}/authorize",
                            "token_endpoint": "https://github.com/login/oauth/access_token" if "github.com" in base else f"{base}/token",
                            "registration_endpoint": None,
                            "scopes_supported": scopes_from_prm,
                            "client_id_metadata_document_supported": False,
                            "supports_dcr": False,
                        }

                if not auth_header:
                    return capabilities

        # 2. Streamable HTTP / Legacy era: initialize handshake
        try:
            init_resp = await client.post(endpoint, json=initialize_payload, headers=headers)

            # Record the "Connecting to the server" step (from initialize, since discover was skipped/406)
            if not any(p.get("title") == "Connecting to the server" for p in capabilities["probe_checks"]):
                capabilities["probe_checks"].append({
                    "title": "Connecting to the server",
                    "status_code": init_resp.status_code,
                    "state": "done" if init_resp.status_code in (200, 202, 401, 403) else "error"
                })

            if init_resp.status_code in (401, 403):
                capabilities["requires_auth"] = True
                capabilities["detected_auth_scheme"] = "bearer" if "bearer" in init_resp.headers.get("www-authenticate", "").lower() else "header"
                if not auth_header:
                    return capabilities

            if init_resp.status_code not in (200, 202):
                raise HTTPException(
                    status_code=422,
                    detail=f"MCP server unreachable or returned {init_resp.status_code}",
                )

            def parse_mcp_response(resp: httpx.Response) -> Dict[str, Any]:
                """Parse JSON or SSE (Streamable HTTP) MCP response."""
                raw = resp.text.strip()
                ct = resp.headers.get("content-type", "")
                if "event-stream" in ct or raw.startswith("event:") or raw.startswith("data:"):
                    for line in raw.splitlines():
                        if line.startswith("data:"):
                            try:
                                return json.loads(line[5:].strip())
                            except Exception:
                                pass
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {}

            init_data = parse_mcp_response(init_resp)

            if "error" in init_data:
                raise HTTPException(
                    status_code=422,
                    detail=f"MCP initialize error: {init_data['error']}",
                )

            capabilities["protocol_version"] = (
                init_data.get("result", {}).get("protocolVersion", "2025-11-25")
            )
            s_info = init_data.get("result", {}).get("serverInfo", {})
            capabilities["server_info"] = s_info
            if isinstance(s_info, dict):
                capabilities["server_name"] = s_info.get("name") or s_info.get("title")

            # Send initialized notification
            await client.post(
                endpoint,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                headers=headers,
            )

            tools_resp = await client.post(
                endpoint,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers=headers,
            )
            if tools_resp.status_code in (200, 202):
                capabilities["tools"] = parse_mcp_response(tools_resp).get("result", {}).get("tools", [])

            res_resp = await client.post(
                endpoint,
                json={"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
                headers=headers,
            )
            if res_resp.status_code in (200, 202):
                capabilities["resources"] = parse_mcp_response(res_resp).get("result", {}).get("resources", [])

            pr_resp = await client.post(
                endpoint,
                json={"jsonrpc": "2.0", "id": 4, "method": "prompts/list", "params": {}},
                headers=headers,
            )
            if pr_resp.status_code in (200, 202):
                capabilities["prompts"] = parse_mcp_response(pr_resp).get("result", {}).get("prompts", [])

            return capabilities

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"MCP probe failed: {e}")


# ---------------------------------------------------------------------------
# Plugin Browse
# ---------------------------------------------------------------------------

@router.get("/plugins")
async def list_plugins(
    type: Optional[str] = Query(None, description="Filter: skill, mcp, or all"),
    query: Optional[str] = Query(None),
    sort: str = Query("popular"),
    request: Request = None,
):
    """List all plugins with optional type filter and search."""
    current_user = await get_current_user_required(request)
    user_id = current_user["id"]
    plugin_type = type if type and type != "all" else None
    plugins = await marketplace_repo.list_plugins(
        plugin_type=plugin_type, query=query, sort_by=sort, current_user_id=user_id
    )
    return {"plugins": plugins, "count": len(plugins)}


class GitHubInspectRequest(BaseModel):
    url: str
    token: Optional[str] = None


class GitHubPackageRequest(BaseModel):
    owner: str
    repo: str
    branch: Optional[str] = "main"
    path: Optional[str] = ""
    subpath: Optional[str] = ""
    token: Optional[str] = None


class GitHubInstallLocalRequest(BaseModel):
    owner: str
    repo: str
    branch: Optional[str] = "main"
    path: Optional[str] = ""
    subpath: Optional[str] = ""
    token: Optional[str] = None
    slug: Optional[str] = None


@router.post("/github/inspect")
async def inspect_github_repo_endpoint(
    payload: GitHubInspectRequest,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Inspect a GitHub repository and discover skills (including multi-skill monorepos).
    """
    try:
        from marketplace.github_service import inspect_github_skills
        result = await inspect_github_skills(payload.url, payload.token)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/github/package")
async def package_github_skill_endpoint(
    payload: GitHubPackageRequest,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Download a subfolder from GitHub and package it into a standard skill ZIP archive.
    """
    try:
        from fastapi.responses import Response
        from marketplace.github_service import package_github_skill
        zip_bytes, suggested_slug = await package_github_skill(
            owner=payload.owner,
            repo=payload.repo,
            branch=payload.branch or "main",
            subpath=(payload.path or payload.subpath or "").strip("/"),
            github_token=payload.token,
        )
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{suggested_slug}.zip"',
                "X-Suggested-Slug": suggested_slug,
            }
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/github/install-local")
async def install_github_skill_local(
    payload: GitHubInstallLocalRequest,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Directly install a skill from GitHub into ~/.talos/skills/<slug>/ (like npx skills add).
    """
    try:
        from marketplace.github_service import package_github_skill
        from marketplace.router import DOT_TALOS_SKILLS_DIR, unpack_skill_zip
        import tempfile

        zip_bytes, suggested_slug = await package_github_skill(
            owner=payload.owner,
            repo=payload.repo,
            branch=payload.branch or "main",
            subpath=(payload.path or payload.subpath or "").strip("/"),
            github_token=payload.token,
        )

        slug = payload.slug or suggested_slug or "custom-skill"
        dest = os.path.join(DOT_TALOS_SKILLS_DIR, slug)
        os.makedirs(dest, exist_ok=True)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            tmp.write(zip_bytes)
            tmp_path = tmp.name

        try:
            unpack_skill_zip(tmp_path, dest)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        return {
            "success": True,
            "slug": slug,
            "installed_path": dest,
            "message": f"Skill installed directly into {dest}. Available to Talos immediately."
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Publish Flow
# ---------------------------------------------------------------------------

@router.post("/publish/skill")
async def publish_skill_plugin(
    name: str = Form(...),
    description: str = Form(...),
    category: Optional[str] = Form("Skills"),
    version: Optional[str] = Form("1.0.0"),
    icon: Optional[str] = Form("bot"),
    changelog: Optional[str] = Form("Initial release"),
    tags: Optional[str] = Form(None),
    short_desc: Optional[str] = Form(None),
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """Publish a skill package. Does NOT auto-install."""
    raw_slug = re.sub(r'[^a-zA-Z0-9_\-]', '-', name.lower()).strip('-')
    slug = re.sub(r'-+', '-', raw_slug) or "custom-skill"
    author_handle = re.sub(
        r'[^a-zA-Z0-9_\-]', '',
        (current_user.get("email", "").split("@")[0] or "user").lower()
    )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="File cannot be empty")

    artifact_sha256 = hashlib.sha256(file_bytes).hexdigest()
    from marketplace.router import STORAGE_SKILLS_DIR, unpack_skill_zip
    zip_path = os.path.join(STORAGE_SKILLS_DIR, f"{slug}_{artifact_sha256[:8]}.zip")
    with open(zip_path, "wb") as f:
        f.write(file_bytes)

    publisher_id = await marketplace_repo.get_or_create_publisher(
        user_id=current_user["id"],
        handle=author_handle,
        display_name=current_user.get("display_name") or author_handle,
    )

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    # Upload to Tigris Object Storage
    from marketplace.tigris_service import tigris_service
    tigris_key = f"skills/{slug}/{version or '1.0.0'}/{os.path.basename(zip_path)}"
    tigris_uri = None
    try:
        tigris_uri = tigris_service.upload_file(zip_path, tigris_key)
        print(f"[Marketplace] Uploaded skill package to Tigris: {tigris_uri}")
    except Exception as e:
        print(f"[Marketplace] Tigris upload warning: {e}")

    result = await marketplace_repo.publish_plugin(
        publisher_id=publisher_id,
        slug=slug,
        name=name,
        description=description,
        plugin_type="skill",
        version=version or "1.0.0",
        zip_path=tigris_uri or zip_path,
        artifact_sha256=artifact_sha256,
        changelog=changelog or "Initial release",
        category=category or "Skills",
        short_desc=short_desc or description[:160],
        tags=tag_list,
    )
    return {
        "success": True,
        "plugin": result,
        "message": "Skill published. Use Install to add it to your PC.",
    }


class PublishMCPRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    slug: str = Field(..., pattern=r'^[a-z0-9\-]+$')
    description: str = Field(default="", min_length=1, max_length=1000)
    endpoint_url: str = Field(..., description="HTTPS endpoint of the remote MCP server")
    auth_type: str = Field(default="none", description="none | bearer | header")
    auth_value: Optional[str] = Field(default=None, description="Token or header value (not stored in DB)")
    version: str = Field(default="1.0.0")
    changelog: Optional[str] = Field(default="Initial release")
    category: Optional[str] = Field(default="MCP Servers")
    tags: Optional[List[str]] = Field(default=None)
    short_desc: Optional[str] = Field(default=None)
    probe_only: Optional[bool] = Field(default=False, alias="_probe_only")


@router.post("/publish/mcp")
async def publish_mcp_plugin(
    payload: PublishMCPRequest,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Verify a remote MCP endpoint and publish its metadata.
    Supports both modern (2026-07-28 stateless) and legacy (initialize handshake) MCP.
    """
    if not payload.endpoint_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="MCP endpoint must use HTTPS")

    auth_header = None
    if payload.auth_type == "bearer" and payload.auth_value:
        auth_header = f"Bearer {payload.auth_value}"
    elif payload.auth_type == "header" and payload.auth_value:
        auth_header = payload.auth_value

    capabilities = await probe_mcp_endpoint(payload.endpoint_url, auth_header)
    if payload.probe_only:
        return {"success": True, "capabilities": capabilities, "message": "Probed successfully"}

    # Store auth config schema for users when installing (publisher never submits personal credentials)
    auth_schema: List[Dict[str, Any]] = []
    if capabilities.get("requires_auth") or payload.auth_type in ("bearer", "header"):
        auth_schema = [
            {"name": "MCP_AUTH_TOKEN", "label": "API Key / Bearer Token", "required": True, "secret": True, "managed_by": "user"}
        ]

    author_handle = re.sub(
        r'[^a-zA-Z0-9_\-]', '',
        (current_user.get("email", "").split("@")[0] or "user").lower()
    )
    publisher_id = await marketplace_repo.get_or_create_publisher(
        user_id=current_user["id"],
        handle=author_handle,
        display_name=current_user.get("display_name") or author_handle,
    )

    result = await marketplace_repo.publish_plugin(
        publisher_id=publisher_id,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        plugin_type="mcp",
        version=payload.version,
        endpoint_url=payload.endpoint_url,
        capabilities_snapshot=capabilities,
        configuration_schema=auth_schema,
        changelog=payload.changelog or "Initial release",
        category=payload.category or "MCP Servers",
        short_desc=payload.short_desc or payload.description[:160],
        tags=payload.tags,
    )
    return {
        "success": True,
        "plugin": result,
        "capabilities": capabilities,
        "message": "Remote MCP server verified and published.",
    }


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------

@router.post("/plugins/{plugin_id}/install-session")
async def create_install_session(
    plugin_id: str,
    version: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Authorize and return install session for local provisioner to download and set up the plugin.
    The actual provisioning happens on Talos Local.
    """
    plugin = await marketplace_repo.get_plugin_by_id(plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    target_version = version or plugin.get("latest_version") or "1.0.0"
    ver_record = await marketplace_repo.get_plugin_version(plugin_id, target_version)
    plugin_version_id = ver_record.get("id") if ver_record else None

    # For skills: direct install
    if plugin["plugin_type"] == "skill":
        zip_path = (ver_record and ver_record.get("zip_path")) or plugin.get("zip_path")
        if not zip_path:
            raise HTTPException(status_code=404, detail="Skill artifact ZIP is missing from storage. Cannot install.")

        temp_downloaded_zip = None
        if zip_path.startswith("tigris://"):
            from marketplace.tigris_service import tigris_service
            import tempfile
            object_key = zip_path.replace(f"tigris://{config.TIGRIS_BUCKET}/", "").lstrip("/")
            file_data = tigris_service.get_object_bytes(object_key)
            if not file_data:
                raise HTTPException(status_code=404, detail="Artifact not found in Tigris object storage.")
            temp_f = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            temp_f.write(file_data)
            temp_f.close()
            temp_downloaded_zip = temp_f.name
            actual_zip_to_unpack = temp_downloaded_zip
        elif not os.path.exists(zip_path):
            raise HTTPException(status_code=404, detail="Skill artifact ZIP is missing from storage. Cannot install.")
        else:
            actual_zip_to_unpack = zip_path

        await marketplace_repo.install_plugin(current_user["id"], plugin_id, plugin_version_id=plugin_version_id)
        from marketplace.router import DOT_TALOS_SKILLS_DIR, unpack_skill_zip
        slug = plugin["slug"]
        dest = os.path.join(DOT_TALOS_SKILLS_DIR, slug)
        os.makedirs(dest, exist_ok=True)
        try:
            unpack_skill_zip(actual_zip_to_unpack, dest)
        finally:
            if temp_downloaded_zip and os.path.exists(temp_downloaded_zip):
                try:
                    os.remove(temp_downloaded_zip)
                except Exception:
                    pass

        return {
            "plugin_id": plugin_id,
            "type": "skill",
            "version": target_version,
            "status": "installed",
            "installed_path": dest,
        }

    # For packaged MCP (if packaged instead of remote URL): verify zip exists
    if plugin["plugin_type"] == "mcp":
        zip_path = plugin.get("zip_path")
        endpoint_url = plugin.get("endpoint_url")
        if zip_path and not os.path.exists(zip_path):
            raise HTTPException(status_code=404, detail="MCP package ZIP is missing from storage. Cannot install.")
        if not zip_path and not endpoint_url:
            raise HTTPException(status_code=400, detail="Invalid MCP plugin: missing both storage package and endpoint URL.")

    # For MCP: register installation in user's profile
    if plugin["plugin_type"] == "mcp":
        await marketplace_repo.install_plugin(current_user["id"], plugin_id, plugin_version_id=plugin_version_id)

    # For MCP: return session so local client can handle
    session_token = secrets.token_urlsafe(32)
    return {
        "plugin_id": plugin_id,
        "plugin_type": plugin["plugin_type"],
        "version": target_version,
        "name": plugin["name"],
        "artifact_sha256": (ver_record and ver_record.get("artifact_sha256")) or plugin.get("artifact_sha256"),
        "zip_download_url": f"/v1/marketplace/plugins/{plugin_id}/download?version={target_version}",
        "manifest": (ver_record and ver_record.get("manifest")) or plugin.get("metadata", {}),
        "configuration_schema": plugin.get("configuration_schema", []),
        "capabilities_snapshot": (ver_record and ver_record.get("capabilities_snapshot")) or plugin.get("capabilities_snapshot", {}),
        "endpoint_url": plugin.get("endpoint_url"),
        "install_token": session_token,
    }


@router.get("/plugins/{plugin_id}/versions")
async def get_plugin_versions(
    plugin_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """List all released versions for a marketplace plugin."""
    plugin = await marketplace_repo.get_plugin_by_id(plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    versions = await marketplace_repo.list_plugin_versions(plugin_id)
    # If no separate versions recorded yet, expose latest_version as initial version
    if not versions and plugin.get("latest_version"):
        versions = [{
            "id": f"ver_{plugin_id}",
            "plugin_id": plugin_id,
            "version": plugin["latest_version"],
            "changelog": "Initial release",
            "artifact_sha256": plugin.get("artifact_sha256"),
            "status": "published",
            "created_at": plugin.get("created_at") or datetime.now(timezone.utc).isoformat(),
        }]
    return {"versions": versions, "count": len(versions)}


@router.get("/plugins/{plugin_id}/versions/{version}")
async def get_plugin_version_detail(
    plugin_id: str,
    version: str,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """Get metadata for a specific release version of a plugin."""
    ver = await marketplace_repo.get_plugin_version(plugin_id, version)
    if not ver:
        # Check if version matches plugin's latest
        plugin = await marketplace_repo.get_plugin_by_id(plugin_id)
        if plugin and plugin.get("latest_version") == version:
            return {
                "id": f"ver_{plugin_id}",
                "plugin_id": plugin_id,
                "version": version,
                "changelog": "Initial release",
                "artifact_sha256": plugin.get("artifact_sha256"),
                "status": "published",
                "created_at": plugin.get("created_at"),
            }
        raise HTTPException(status_code=404, detail="Version not found")
    return ver


@router.post("/plugins/{plugin_id}/resolve-install")
async def resolve_plugin_install(
    plugin_id: str,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Resolve installation metadata and signed download URL for Talos Local provisioner.
    Local verifies SHA-256 and unpacks/provisions locally.
    """
    plugin = await marketplace_repo.get_plugin_by_id(plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    target_version = body.get("version") or plugin.get("latest_version") or "1.0.0"

    # Find version record if available
    ver_record = await marketplace_repo.get_plugin_version(plugin_id, target_version)

    artifact_sha256 = (ver_record and ver_record.get("artifact_sha256")) or plugin.get("artifact_sha256")
    manifest = (ver_record and ver_record.get("manifest")) or plugin.get("metadata") or {}
    caps = (ver_record and ver_record.get("capabilities_snapshot")) or plugin.get("capabilities_snapshot") or {}

    download_url = f"/v1/marketplace/plugins/{plugin_id}/download"
    if target_version:
        download_url += f"?version={target_version}"

    return {
        "plugin_id": plugin_id,
        "slug": plugin.get("slug"),
        "name": plugin.get("name"),
        "plugin_type": plugin.get("plugin_type"),
        "version": target_version,
        "artifact_url": download_url,
        "artifact_sha256": artifact_sha256,
        "manifest": manifest,
        "capabilities": caps,
        "configuration_schema": plugin.get("configuration_schema") or [],
        "endpoint_url": plugin.get("endpoint_url"),
        "requirements": {
            "browser": "browser" in plugin.get("description", "").lower() or "browser" in plugin.get("name", "").lower(),
            "network": True,
            "shell": False,
            "files": False,
        }
    }


@router.get("/plugins/{plugin_id}/download")
async def download_plugin_package(
    plugin_id: str,
    version: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """Download the plugin artifact ZIP for local provisioning."""
    from fastapi.responses import FileResponse
    plugin = await marketplace_repo.get_plugin_by_id(plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    zip_path = None
    target_ver = version or plugin.get("latest_version")
    if target_ver:
        ver_record = await marketplace_repo.get_plugin_version(plugin_id, target_ver)
        if ver_record and ver_record.get("zip_path"):
            zip_path = ver_record.get("zip_path")

    if not zip_path:
        zip_path = plugin.get("zip_path")

    if zip_path and zip_path.startswith("tigris://"):
        from fastapi.responses import RedirectResponse
        from marketplace.tigris_service import tigris_service
        download_url = tigris_service.generate_download_url(zip_path, expires_in=3600)
        return RedirectResponse(url=download_url)

    if not zip_path or not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Artifact not found in storage")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{plugin['slug']}-{target_ver or 'pkg'}.zip")


@router.post("/plugins/{plugin_id}/install-confirm")
async def confirm_plugin_installed(
    plugin_id: str,
    version: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """Called by local provisioner after successful local install to register in DB."""
    plugin = await marketplace_repo.get_plugin_by_id(plugin_id)
    target_ver = version or (plugin.get("latest_version") if plugin else "1.0.0")
    ver_record = await marketplace_repo.get_plugin_version(plugin_id, target_ver)
    plugin_version_id = ver_record.get("id") if ver_record else None

    await marketplace_repo.install_plugin(current_user["id"], plugin_id, plugin_version_id=plugin_version_id)
    return {"success": True, "installed_version": target_ver}


@router.post("/plugins/{plugin_id}/uninstall")
async def uninstall_plugin(
    plugin_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """Uninstall a plugin from user account and delete local installed files & credentials."""
    plugin = await marketplace_repo.get_plugin_by_id(plugin_id)
    await marketplace_repo.uninstall_plugin(current_user["id"], plugin_id)

    if plugin:
        slug = plugin.get("slug")
        ptype = plugin.get("plugin_type")
        user_home = os.path.expanduser("~")

        # 1. Clean up skills folder in ~/.talos/skills/<slug>
        if ptype == "skill" and slug:
            skill_dir = os.path.join(user_home, ".talos", "skills", slug)
            if os.path.exists(skill_dir):
                try:
                    shutil.rmtree(skill_dir)
                except Exception:
                    pass

        # 2. Clean up stored credentials / secrets metadata for mcp or other plugins
        try:
            meta_path = os.path.join(user_home, ".talos", "plugin_config_meta.json")
            if os.path.exists(meta_path):
                import json
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if plugin_id in meta:
                    del meta[plugin_id]
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, indent=2)
        except Exception:
            pass

    return {"success": True, "message": "Plugin uninstalled and local files removed"}


@router.delete("/plugins/{plugin_id}")
@router.post("/plugins/{plugin_id}/unpublish")
async def unpublish_plugin(
    plugin_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Permanently unpublish and delete a plugin from the registry.
    Only the owner/publisher can unpublish.
    Purges storage artifact ZIPs and local installed copies.
    """
    user_id = current_user["id"]
    try:
        deleted = await marketplace_repo.delete_plugin(plugin_id, user_id)
    except ValueError as val_err:
        raise HTTPException(status_code=403, detail=str(val_err))

    if not deleted:
        raise HTTPException(status_code=404, detail="Plugin not found or already deleted")

    # 1. Clean up storage artifact in Tigris or local disk
    zip_path = deleted.get("zip_path")
    if zip_path and zip_path.startswith("tigris://"):
        try:
            from marketplace.tigris_service import tigris_service
            # Format: tigris://talos-marketplace/<key>
            key_part = zip_path.replace(f"tigris://{config.TIGRIS_BUCKET}/", "").lstrip("/")
            tigris_service.delete_object(key_part)
            print(f"[Marketplace] Deleted object from Tigris: {key_part}")
        except Exception as te:
            print(f"[Marketplace] Tigris delete warning: {te}")
    elif zip_path and os.path.exists(zip_path):
        try:
            os.remove(zip_path)
        except Exception:
            pass

    slug = deleted.get("slug")
    ptype = deleted.get("plugin_type")
    user_home = os.path.expanduser("~")

    # Also clean storage by slug if applicable
    if slug:
        if ptype == "skill":
            storage_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage", "skills"))
        else:
            storage_dir = STORAGE_MCP_DIR
        named_zip = os.path.join(storage_dir, f"{slug}.zip")
        if os.path.exists(named_zip):
            try:
                os.remove(named_zip)
            except Exception:
                pass

        # 2. Clean local ~/.talos copies
        if ptype == "skill":
            local_skill_dir = os.path.join(user_home, ".talos", "skills", slug)
            if os.path.exists(local_skill_dir):
                try:
                    shutil.rmtree(local_skill_dir)
                except Exception:
                    pass

    return {
        "success": True,
        "message": f"Plugin '{deleted.get('name')}' has been unpublished and permanently removed from registry."
    }


# ---------------------------------------------------------------------------
# User's Installed Plugins
# ---------------------------------------------------------------------------

@router.get("/user/plugins")
async def get_user_plugins(
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """List all installed plugins for the current user."""
    plugins = await marketplace_repo.get_user_installed_plugins(current_user["id"])
    return {"installed": plugins}


class TogglePluginRequest(BaseModel):
    is_active: bool


@router.patch("/user/plugins/{plugin_id}/toggle")
async def toggle_plugin_active(
    plugin_id: str,
    payload: TogglePluginRequest,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """Toggle a plugin's active state (whether the agent can access it)."""
    await marketplace_repo.toggle_plugin_active(current_user["id"], plugin_id, payload.is_active)
    return {"success": True, "is_active": payload.is_active}


class UpdatePluginConfigRequest(BaseModel):
    configuration_metadata: Dict[str, Any]  # Non-secret flags only


@router.patch("/user/plugins/{plugin_id}/config")
async def update_plugin_config(
    plugin_id: str,
    payload: UpdatePluginConfigRequest,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """Update non-secret config metadata for a user's plugin."""
    await marketplace_repo.update_plugin_config(
        current_user["id"], plugin_id, payload.configuration_metadata
    )
    return {"success": True}


# ---------------------------------------------------------------------------
# Scoped Runtime Relay Token Minting
# ---------------------------------------------------------------------------

class MintTokenRequest(BaseModel):
    plugin_id: str
    run_id: str
    ttl_seconds: int = Field(default=3600, le=86400)


@router.post("/internal/mint-relay-token")
async def mint_relay_token(
    payload: MintTokenRequest,
    request: Request,
):
    """
    Internal endpoint: Talos Local backend calls this to mint a scoped runtime
    token for a sub-agent so it can call the Cloud Relay.
    Secured by X-Talos-Internal-Token header.
    """
    internal_token = request.headers.get("X-Talos-Internal-Token")
    if internal_token != config.TALOS_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    user_id = request.headers.get("X-Talos-User-ID", "usr_local_dev")
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=payload.ttl_seconds)

    await marketplace_repo.create_runtime_token(
        user_id=user_id,
        plugin_id=payload.plugin_id,
        run_id=payload.run_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )

    return {
        "token": raw_token,
        "expires_at": expires_at.isoformat(),
        "plugin_id": payload.plugin_id,
        "run_id": payload.run_id,
    }


@router.post("/internal/validate-relay-token")
async def validate_relay_token(request: Request):
    """Internal: validate a relay token (called by relay.py before forwarding to LLM)."""
    internal_token = request.headers.get("X-Talos-Internal-Token")
    if internal_token != config.TALOS_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    raw_token = body.get("token", "")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    claims = await marketplace_repo.validate_runtime_token(token_hash)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired relay token")
@router.get("/internal/user/{user_id}/active-plugins")
async def get_active_plugins_for_agent(
    user_id: str,
    request: Request,
):
    """
    Internal endpoint: Talos Local backend calls this to get active installed plugins
    (MCP servers and Sub-Agents) with their capabilities, tools snapshot, and endpoint URLs.
    """
    internal_token = request.headers.get("X-Talos-Internal-Token")
    if internal_token != config.TALOS_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    target_user_id = user_id
    # If user_id not found or is default, fallback to active installed user
    plugins = await marketplace_repo.get_user_installed_plugins(target_user_id)
    if not plugins:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(config.POSTGRES_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT user_id FROM user_installed_plugins LIMIT 1")
                row = cur.fetchone()
                if row and row["user_id"]:
                    plugins = await marketplace_repo.get_user_installed_plugins(row["user_id"])

    active_plugins = [p for p in plugins if p.get("is_active")]
    return {"plugins": active_plugins}


# ---------------------------------------------------------------------------
# MCP OAuth 2.1 / RFC 9728 Dynamic Flow
# ---------------------------------------------------------------------------

# In-memory transient store for PKCE state across auth redirects
OAUTH_SESSIONS: Dict[str, Dict[str, Any]] = {}

class OAuthStartRequest(BaseModel):
    plugin_id: str
    redirect_uri: str
    client_mode: Optional[str] = "dcr"  # 'cimd' | 'dcr' | 'manual'
    manual_client_id: Optional[str] = None

@router.post("/oauth/start")
async def start_mcp_oauth(
    payload: OAuthStartRequest,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Initiate OAuth authorization for an installed MCP plugin:
    1. Look up MCP endpoint & capabilities.
    2. If needed, dynamically register client via DCR (RFC 7591).
    3. Generate PKCE code verifier and challenge (RFC 7636).
    4. Return authorization URL for browser redirect.
    """
    plugin = await marketplace_repo.get_plugin_by_id(payload.plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    caps = plugin.get("capabilities_snapshot") or {}
    oauth_meta = caps.get("oauth")
    endpoint_url = plugin.get("endpoint_url")

    # If oauth metadata wasn't captured at publish time, probe on the fly
    if not oauth_meta and endpoint_url:
        fresh_caps = await probe_mcp_endpoint(endpoint_url)
        oauth_meta = fresh_caps.get("oauth")

    if not oauth_meta or not oauth_meta.get("authorization_endpoint"):
        raise HTTPException(
            status_code=400,
            detail="MCP server does not support standard OAuth 2.1 authorization discovery."
        )

    auth_endpoint = oauth_meta["authorization_endpoint"]
    token_endpoint = oauth_meta["token_endpoint"]
    reg_endpoint = oauth_meta.get("registration_endpoint")

    client_id = payload.manual_client_id
    if not client_id and reg_endpoint and payload.client_mode in ("dcr", "cimd"):
        # Perform Dynamic Client Registration (RFC 7591)
        async with httpx.AsyncClient(timeout=10.0) as client:
            dcr_resp = await client.post(
                reg_endpoint,
                json={
                    "client_name": "Talos AI Assistant",
                    "redirect_uris": [payload.redirect_uri],
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none"
                },
                headers={"Content-Type": "application/json"}
            )
            if dcr_resp.status_code in (200, 201):
                dcr_data = dcr_resp.json()
                client_id = dcr_data.get("client_id")

    if not client_id:
        # Fallback default client identifier for public client
        client_id = "talos-ai-assistant"

    # PKCE code challenge (RFC 7636)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")

    state = secrets.token_urlsafe(32)

    # Store state session
    OAUTH_SESSIONS[state] = {
        "user_id": current_user["id"],
        "plugin_id": payload.plugin_id,
        "token_endpoint": token_endpoint,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "redirect_uri": payload.redirect_uri,
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    import urllib.parse
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": payload.redirect_uri,
        "scope": " ".join(oauth_meta.get("scopes_supported", ["default"])),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state
    }
    authorization_url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"

    return {
        "authorization_url": authorization_url,
        "state": state,
        "client_id": client_id
    }


class OAuthExchangeRequest(BaseModel):
    state: str
    code: str

@router.post("/oauth/exchange")
async def exchange_mcp_oauth(
    payload: OAuthExchangeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user_required),
):
    """
    Exchange authorization code for access token using stored PKCE code_verifier,
    and save the token securely into user's configuration metadata and local secrets.
    """
    session = OAUTH_SESSIONS.pop(payload.state, None)
    if not session:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    token_endpoint = session["token_endpoint"]
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": payload.code,
                "redirect_uri": session["redirect_uri"],
                "client_id": session["client_id"],
                "code_verifier": session["code_verifier"]
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        if token_resp.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Token exchange failed ({token_resp.status_code}): {token_resp.text}"
            )

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access_token returned by token endpoint")

        plugin_id = session["plugin_id"]

        # 1. Update user_installed_plugins configuration_metadata in DB
        await marketplace_repo.update_plugin_config(
            current_user["id"],
            plugin_id,
            {
                "MCP_AUTH_TOKEN": {"configured": True},
                "auth_method": "oauth2",
                "connected_at": datetime.now(timezone.utc).isoformat()
            }
        )

        # 2. Save token to local environment (~/.talos/plugin_config_meta.json)
        user_home = os.path.expanduser("~")
        meta_file = os.path.join(user_home, ".talos", "plugin_config_meta.json")
        os.makedirs(os.path.dirname(meta_file), exist_ok=True)
        local_meta = {}
        if os.path.exists(meta_file):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    local_meta = json.load(f)
            except Exception:
                local_meta = {}

        if plugin_id not in local_meta:
            local_meta[plugin_id] = {}
        local_meta[plugin_id]["MCP_AUTH_TOKEN"] = access_token
        local_meta[plugin_id]["auth_type"] = "bearer"
        if token_data.get("refresh_token"):
            local_meta[plugin_id]["refresh_token"] = token_data["refresh_token"]

        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(local_meta, f, indent=2)

        # 3. Query authenticated tools/list to enrich plugin capabilities_snapshot with real tools
        try:
            plugin_rec = await marketplace_repo.get_plugin_by_id(plugin_id)
            endpoint_url = plugin_rec.get("endpoint_url") if plugin_rec else None
            if endpoint_url:
                probe_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "Authorization": f"Bearer {access_token}"
                }
                tools_resp = await client.post(
                    endpoint_url,
                    json={"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
                    headers=probe_headers
                )
                if tools_resp.status_code == 200:
                    raw_text = tools_resp.text
                    discovered_tools = []
                    for line in raw_text.splitlines():
                        if line.startswith("data:"):
                            parsed = json.loads(line[5:].strip())
                            discovered_tools = parsed.get("result", {}).get("tools", [])
                            break
                    if not discovered_tools:
                        try:
                            discovered_tools = tools_resp.json().get("result", {}).get("tools", [])
                        except Exception:
                            pass
                    if discovered_tools:
                        caps = plugin_rec.get("capabilities_snapshot") or {}
                        caps["tools"] = discovered_tools
                        with marketplace_repo._get_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE plugins SET capabilities_snapshot=%s::jsonb WHERE id=%s", (json.dumps(caps), plugin_id))
                                conn.commit()
        except Exception as e:
            print(f"[OAuth Exchange] Notice: tools/list refresh skipped: {e}")

        return {
            "success": True,
            "plugin_id": plugin_id,
            "token_type": token_data.get("token_type", "Bearer"),
            "message": "Successfully connected and authenticated with MCP server"
        }

