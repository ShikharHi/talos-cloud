import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File, Form
from typing import List, Optional
from auth.service import get_current_user
from sync.repository import sync_repo
from sync.models import RegisterKeysRequest, CheckChunksRequest, CheckChunksResponse, CommitManifestRequest
from marketplace.tigris_service import TigrisStorageService

router = APIRouter(prefix="/v1/sync", tags=["sync"])
tigris_service = TigrisStorageService()

@router.post("/keys")
async def register_user_keys(req: RegisterKeysRequest, current_user: dict = Depends(get_current_user)):
    """Store client-encrypted DEK envelope and KDF salt."""
    await sync_repo.set_user_keys(
        user_id=current_user["id"],
        wrapped_dek=req.wrapped_dek,
        salt=req.salt,
        recovery_wrapped_dek=req.recovery_wrapped_dek,
        recovery_salt=req.recovery_salt
    )
    return {"status": "success", "message": "Zero-knowledge key envelope stored"}

@router.get("/keys")
async def get_user_keys(current_user: dict = Depends(get_current_user)):
    """Fetch user's wrapped DEK envelope for decryption on a new device."""
    keys = await sync_repo.get_user_keys(current_user["id"])
    if not keys:
        raise HTTPException(status_code=404, detail="No sync keys found for user")
    return {"status": "success", "keys": keys}

@router.post("/chunks/check", response_model=CheckChunksResponse)
async def check_missing_chunks(req: CheckChunksRequest, current_user: dict = Depends(get_current_user)):
    """Deduplication query: returns which chunk IDs are missing and need upload."""
    existing = await sync_repo.find_existing_chunks(current_user["id"], req.chunk_ids)
    missing = [cid for cid in req.chunk_ids if cid not in existing]
    return CheckChunksResponse(missing_chunk_ids=missing)

@router.post("/chunks/upload")
async def upload_chunk(
    chunk_id: str = Form(...),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """Upload encrypted, compressed chunk to Tigris S3."""
    user_id = current_user["id"]
    content = await file.read()
    object_key = f"sync/{user_id}/chunks/{chunk_id}.bin"
    
    # Store directly in Tigris
    tigris_service.upload_bytes(content, object_key, content_type="application/octet-stream")
    
    # Record in database
    await sync_repo.record_chunks(user_id, [{
        "chunk_id": chunk_id,
        "storage_key": object_key,
        "size_bytes": len(content)
    }])
    return {"status": "success", "chunk_id": chunk_id, "size": len(content)}

@router.get("/chunks/{chunk_id}")
async def get_chunk_download(chunk_id: str, current_user: dict = Depends(get_current_user)):
    """Generate secure download URL for a chunk."""
    user_id = current_user["id"]
    object_key = f"sync/{user_id}/chunks/{chunk_id}.bin"
    url = tigris_service.generate_download_url(object_key, expires_in=1800)
    return {"status": "success", "chunk_id": chunk_id, "download_url": url}

@router.post("/manifests/commit")
async def commit_sync_manifest(req: CommitManifestRequest, current_user: dict = Depends(get_current_user)):
    """Atomically commit a versioned manifest with parent vector clock check."""
    user_id = current_user["id"]
    manifest_id = f"man_{uuid.uuid4().hex[:12]}"
    
    await sync_repo.commit_manifest(
        manifest_id=manifest_id,
        thread_id=req.thread_id,
        user_id=user_id,
        device_id=req.device_id,
        sequence_number=req.sequence_number,
        parent_manifest_id=req.parent_manifest_id,
        encrypted_manifest=req.encrypted_manifest
    )
    return {"status": "success", "manifest_id": manifest_id, "sequence": req.sequence_number}

@router.get("/manifests/latest/{thread_id}")
async def get_latest_manifest(thread_id: str, current_user: dict = Depends(get_current_user)):
    """Get the most recent manifest for a thread to restore or check for remote updates."""
    manifest = await sync_repo.get_latest_thread_manifest(current_user["id"], thread_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="No sync manifest found for thread")
    return {"status": "success", "manifest": manifest}

@router.get("/manifests/summary")
async def get_all_manifests_summary(current_user: dict = Depends(get_current_user)):
    """Get all thread latest manifests for full device hydration on startup."""
    manifests = await sync_repo.get_all_user_manifests(current_user["id"])
    return {"status": "success", "manifests": manifests}
