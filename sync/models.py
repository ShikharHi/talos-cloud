from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class RegisterKeysRequest(BaseModel):
    wrapped_dek: str       # Hex-encoded AES-GCM wrapped DEK
    salt: str              # Hex-encoded KDF salt
    recovery_wrapped_dek: Optional[str] = None
    recovery_salt: Optional[str] = None
    key_version: int = 1

class CheckChunksRequest(BaseModel):
    chunk_ids: List[str]

class CheckChunksResponse(BaseModel):
    missing_chunk_ids: List[str]

class CommitManifestRequest(BaseModel):
    thread_id: str
    device_id: str
    sequence_number: int
    parent_manifest_id: Optional[str] = None
    encrypted_manifest: str # Hex-encoded AES-GCM encrypted metadata
    chunk_ids: List[str]
