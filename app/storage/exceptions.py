"""
Talos Cloud — Storage exceptions hierarchy.

All storage-layer errors map to subclasses of StorageError.
Exceptions are sanitised so that credentials, internal tokens, or raw boto3 stack traces
are never leaked in exception messages or HTTP responses.
"""

from typing import Optional


class StorageError(Exception):
    """Base exception for all storage-related errors."""

    def __init__(self, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class StorageConfigurationError(StorageError):
    """Raised when storage configuration is missing, incomplete, or invalid."""
    pass


class StorageNotFoundError(StorageError):
    """Raised when a requested object or bucket is not found."""
    pass


class StoragePermissionError(StorageError):
    """Raised when access to a storage resource is forbidden."""
    pass


class StorageUploadError(StorageError):
    """Raised when an object upload fails or cannot be initiated."""
    pass


class StorageDownloadError(StorageError):
    """Raised when an object download or presigned URL creation fails."""
    pass


class StorageValidationError(StorageError):
    """Raised when an uploaded package fails integrity, security, or manifest checks."""
    pass


class StorageImmutabilityError(StorageError):
    """Raised when attempting to overwrite an already published, immutable package version."""
    pass
