"""
Talos Cloud — Storage Models & State Machines.

Defines explicit state machines for package uploads and versions,
along with structured storage metadata models.
"""

from datetime import datetime
from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class UploadState(str, Enum):
    """
    Explicit state machine for in-flight package uploads:
      PENDING   -> Upload record created, presigned PUT URL issued to client
      UPLOADED  -> Client signaled upload completion, object verified present in storage
      VERIFYING -> Server is actively streaming, hashing, and validating package
      COMPLETED -> Verification succeeded, canonical version record created
      FAILED    -> Verification failed or invalid payload
      EXPIRED   -> Upload expired before completion; cleaned up by background task
    """
    PENDING = "pending"
    UPLOADED = "uploaded"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class VersionState(str, Enum):
    """
    Explicit state machine for package versions:
      PENDING  -> Version recorded, awaiting review / verification
      VERIFIED -> Package passed all security, archive, and manifest checks
      APPROVED -> Published and discoverable / installable
      REJECTED -> Rejected by moderation or security policy
    """
    PENDING = "pending"
    VERIFIED = "verified"
    APPROVED = "approved"
    REJECTED = "rejected"


class StorageMetadata(BaseModel):
    """Metadata describing an object stored in object storage."""
    model_config = ConfigDict(from_attributes=True)

    key: str
    bucket: str
    size: int
    content_type: str = "application/octet-stream"
    etag: Optional[str] = None
    sha256: Optional[str] = None
    last_modified: Optional[datetime] = None


class PresignedUploadUrl(BaseModel):
    """Response returned to client to initiate a direct presigned upload."""
    upload_id: str
    object_key: str
    upload_url: str
    expires_in: int
    http_method: str = "PUT"


class PresignedDownloadUrl(BaseModel):
    """Response returned to client to download a package or asset directly."""
    download_url: str
    expires_in: int


class UploadVerificationResult(BaseModel):
    """Result of server-side streaming verification of an uploaded package."""
    valid: bool
    file_size: int = 0
    sha256: str = ""
    mime_type: str = "application/zip"
    manifest_yaml: Optional[str] = None
    errors: List[str] = Field(default_factory=list)
