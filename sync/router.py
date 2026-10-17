import hashlib
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import RedirectResponse
from typing import Optional
from auth.service import get_current_user
from sync.repository import sync_repo, MAX_SNAPSHOT_SIZE_BYTES
from sync.models import SnapshotInfo, PendingDeleteRequest, ResolvePendingDeletesRequest
from marketplace.tigris_service import TigrisStorageService

router = APIRouter(prefix="/v1/snapshot", tags=["snapshot"])
tigris = TigrisStorageService()

# ── Snapshot upload ────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_snapshot(
    file: UploadFile = File(...),
    generation: int = Form(...),
    checksum_sha256: str = Form(...),
    format_version: int = Form(default=1),
    current_user: dict = Depends(get_current_user),
):
    """
    Accepts a .tar.zst snapshot archive.
    - Verifies size limit.
    - Verifies SHA-256 checksum.
    - Uploads to Tigris under an opaque, server-generated key.
    - Commits the snapshot pointer only when incoming generation > current generation.
    - Clears tombstones confirmed included in this snapshot.
    """
    user_id = current_user["id"]

    content = await file.read()
    size = len(content)

    if size > MAX_SNAPSHOT_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Snapshot exceeds 2 GB size limit")

    # Verify checksum
    actual_checksum = hashlib.sha256(content).hexdigest()
    if actual_checksum != checksum_sha256:
        raise HTTPException(
            status_code=400,
            detail=f"Checksum mismatch: expected {checksum_sha256}, got {actual_checksum}"
        )

    # Guard against generation regression (additional safety on top of DB conditional update)
    existing = sync_repo.get_user_snapshot(user_id)
    if existing and existing.get("generation", 0) >= generation:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Snapshot generation {generation} is not newer than committed "
                f"generation {existing['generation']}. Upload rejected."
            )
        )

    # Opaque, server-generated object key — never provided by client
    object_key = f"snapshots/{user_id}/{uuid.uuid4().hex}.tar.zst"

    # Upload to Tigris
    tigris.upload_bytes(content, object_key, content_type="application/zstd")

    # Delete previous object from Tigris if there was one
    if existing and existing.get("object_key"):
        try:
            tigris.delete_object(existing["object_key"])
        except Exception:
            pass  # Non-fatal; old object becomes orphaned but won't be served

    # Commit the snapshot pointer
    snapshot_id = sync_repo.upsert_snapshot(
        user_id=user_id,
        object_key=object_key,
        generation=generation,
        size_bytes=size,
        checksum_sha256=actual_checksum,
        format_version=format_version,
    )

    # Resolve tombstones: clear all pending deletes since this snapshot
    # was built locally (backend already excluded deleted threads from archive)
    pending = sync_repo.get_pending_deletes(user_id)
    if pending:
        sync_repo.resolve_pending_deletes(user_id, pending)

    return {
        "status": "success",
        "snapshot_id": snapshot_id,
        "generation": generation,
        "size_bytes": size,
    }


# ── Snapshot info ──────────────────────────────────────────────────────────────

@router.get("/info", response_model=SnapshotInfo)
async def get_snapshot_info(current_user: dict = Depends(get_current_user)):
    """Returns current snapshot metadata + outstanding tombstones."""
    user_id = current_user["id"]
    record = sync_repo.get_user_snapshot(user_id)
    pending = sync_repo.get_pending_deletes(user_id)

    if not record:
        return SnapshotInfo(has_snapshot=False, pending_deletes=pending)

    return SnapshotInfo(
        has_snapshot=True,
        snapshot_id=record["snapshot_id"],
        generation=record["generation"],
        size_bytes=record["size_bytes"],
        checksum_sha256=record["checksum_sha256"],
        format_version=record["format_version"],
        created_at=record["created_at"],
        committed_at=record["committed_at"],
        pending_deletes=pending,
    )


# ── Snapshot download ──────────────────────────────────────────────────────────

@router.get("/download")
async def download_snapshot(current_user: dict = Depends(get_current_user)):
    """
    Returns a short-lived (15 min) presigned URL for the user's snapshot.
    Authenticates user before generating URL.
    """
    user_id = current_user["id"]
    record = sync_repo.get_user_snapshot(user_id)
    if not record:
        raise HTTPException(status_code=404, detail="No snapshot found for this account")

    signed_url = tigris.generate_download_url(record["object_key"], expires_in=900)
    return {
        "status": "success",
        "download_url": signed_url,
        "expires_in": 900,
        "checksum_sha256": record["checksum_sha256"],
        "size_bytes": record["size_bytes"],
        "generation": record["generation"],
    }


# ── Snapshot delete ────────────────────────────────────────────────────────────

@router.delete("/")
async def delete_snapshot(current_user: dict = Depends(get_current_user)):
    """Permanently wipes the user's cloud backup. Requires explicit action."""
    user_id = current_user["id"]
    record = sync_repo.get_user_snapshot(user_id)
    if record and record.get("object_key"):
        tigris.delete_object(record["object_key"])
    sync_repo.delete_snapshot_record(user_id)
    return {"status": "success", "message": "Snapshot deleted"}


# ── Pending deletes (tombstones) ───────────────────────────────────────────────

@router.post("/pending-deletes")
async def add_pending_delete(
    req: PendingDeleteRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Records a durable tombstone for a deleted thread.
    Idempotent — safe to call multiple times.
    """
    sync_repo.add_pending_delete(current_user["id"], req.thread_id)
    return {"status": "success", "thread_id": req.thread_id}


@router.delete("/pending-deletes")
async def resolve_pending_deletes(
    req: ResolvePendingDeletesRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Clears only the tombstones that are confirmed to be excluded from the
    committed snapshot at the given generation.
    """
    # Only resolve if the committed snapshot generation matches or exceeds req.generation
    record = sync_repo.get_user_snapshot(current_user["id"])
    if not record or record.get("generation", 0) < req.generation:
        raise HTTPException(
            status_code=409,
            detail="Cannot resolve tombstones: committed snapshot generation is older than requested"
        )
    sync_repo.resolve_pending_deletes(current_user["id"], req.thread_ids)
    return {"status": "success", "resolved": len(req.thread_ids)}
