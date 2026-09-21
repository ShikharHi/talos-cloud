"""
Talos Cloud — Storage Module.

Provides provider-independent S3-compatible object storage abstractions.
"""

from app.storage.base import StorageProvider
from app.storage.exceptions import (
    StorageConfigurationError,
    StorageDownloadError,
    StorageError,
    StorageImmutabilityError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageUploadError,
    StorageValidationError,
)
from app.storage.keys import (
    asset_object_key,
    package_object_key,
    sanitize_identifier,
    temp_asset_upload_object_key,
    temp_upload_object_key,
)
from app.storage.models import (
    PresignedDownloadUrl,
    PresignedUploadUrl,
    StorageMetadata,
    UploadState,
    UploadVerificationResult,
    VersionState,
)
from app.storage.s3 import S3StorageProvider
from app.storage.service import StorageService, get_storage_service, reset_storage_service

__all__ = [
    "StorageProvider",
    "S3StorageProvider",
    "StorageService",
    "get_storage_service",
    "reset_storage_service",
    "StorageError",
    "StorageConfigurationError",
    "StorageNotFoundError",
    "StoragePermissionError",
    "StorageUploadError",
    "StorageDownloadError",
    "StorageValidationError",
    "StorageImmutabilityError",
    "UploadState",
    "VersionState",
    "StorageMetadata",
    "PresignedUploadUrl",
    "PresignedDownloadUrl",
    "UploadVerificationResult",
    "package_object_key",
    "temp_upload_object_key",
    "asset_object_key",
    "temp_asset_upload_object_key",
    "sanitize_identifier",
]
