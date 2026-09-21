"""
Talos Cloud — Abstract Storage Provider.

Provider-independent interface for object storage operations.
Implementations (e.g. S3StorageProvider) handle low-level SDK details.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, BinaryIO, Optional
from app.storage.models import StorageMetadata


class StorageProvider(ABC):
    """Abstract provider interface for object storage."""

    @abstractmethod
    async def exists(self, key: str, bucket: Optional[str] = None) -> bool:
        """Check whether an object exists at the given key."""
        pass

    @abstractmethod
    async def get_metadata(self, key: str, bucket: Optional[str] = None) -> StorageMetadata:
        """Retrieve metadata for an existing object."""
        pass

    @abstractmethod
    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> StorageMetadata:
        """Upload raw bytes to the specified key."""
        pass

    @abstractmethod
    async def upload_stream(
        self,
        key: str,
        source_file: BinaryIO,
        content_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> StorageMetadata:
        """Stream data from an open binary file-like object to the specified key."""
        pass

    @abstractmethod
    async def download(self, key: str, bucket: Optional[str] = None) -> bytes:
        """Download complete object contents as bytes."""
        pass

    @abstractmethod
    async def delete(self, key: str, bucket: Optional[str] = None) -> bool:
        """Delete an object from storage. Returns True if deleted."""
        pass

    @abstractmethod
    async def create_upload_url(
        self,
        key: str,
        expires_in: int = 900,
        content_type: Optional[str] = None,
        bucket: Optional[str] = None,
    ) -> str:
        """Generate a short-lived presigned PUT URL for uploading an object."""
        pass

    @abstractmethod
    async def create_download_url(
        self,
        key: str,
        expires_in: int = 900,
        filename: Optional[str] = None,
        bucket: Optional[str] = None,
    ) -> str:
        """Generate a short-lived presigned GET URL for downloading an object."""
        pass

    @abstractmethod
    async def copy(
        self,
        source_key: str,
        dest_key: str,
        bucket: Optional[str] = None,
    ) -> None:
        """Copy an object from source_key to dest_key within the bucket."""
        pass

    @abstractmethod
    async def download_stream_to_file(
        self,
        key: str,
        target_file: BinaryIO,
        bucket: Optional[str] = None,
        chunk_size: int = 65536,
    ) -> int:
        """
        Stream object content from storage into an open binary file-like object.
        Avoids reading the entire object into memory. Returns total bytes streamed.
        """
        pass
