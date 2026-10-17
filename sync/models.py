from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime


class SnapshotUploadRequest(BaseModel):
    """Metadata submitted alongside the multipart archive upload."""
    generation: int = Field(..., description="Monotonically increasing generation number from client")
    checksum_sha256: str = Field(..., description="Hex SHA-256 of the raw archive bytes")
    format_version: int = Field(default=1)
    size_bytes: int


class SnapshotInfo(BaseModel):
    has_snapshot: bool
    snapshot_id: Optional[str] = None
    generation: Optional[int] = None
    size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None
    format_version: Optional[int] = None
    created_at: Optional[datetime] = None
    committed_at: Optional[datetime] = None
    pending_deletes: List[str] = Field(default_factory=list)


class PendingDeleteRequest(BaseModel):
    thread_id: str


class ResolvePendingDeletesRequest(BaseModel):
    """Called after a successful snapshot upload: clears only tombstones
    that were captured in the snapshot with the given generation."""
    generation: int
    thread_ids: List[str]
