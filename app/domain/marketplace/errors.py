"""
Talos Cloud — Marketplace Domain Errors.

Provides stable, machine-readable error codes and error hierarchy.
"""

from __future__ import annotations


class MarketplaceError(Exception):
    """Base error for all marketplace operations."""
    error_code: str = "MARKETPLACE_ERROR"

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ListingNotFoundError(MarketplaceError):
    error_code = "LISTING_NOT_FOUND"


class ListingSlugConflictError(MarketplaceError):
    error_code = "SLUG_CONFLICT"


class VersionNotFoundError(MarketplaceError):
    error_code = "VERSION_NOT_FOUND"


class VersionConflictError(MarketplaceError):
    error_code = "VERSION_CONFLICT"


class VersionImmutableError(MarketplaceError):
    error_code = "PACKAGE_VERSION_IMMUTABLE"


class UploadNotFoundError(MarketplaceError):
    error_code = "UPLOAD_NOT_FOUND"


class UploadExpiredError(MarketplaceError):
    error_code = "UPLOAD_EXPIRED"


class UploadAlreadyCompletedError(MarketplaceError):
    error_code = "UPLOAD_ALREADY_COMPLETED"


class UploadNotOwnedError(MarketplaceError):
    error_code = "UPLOAD_NOT_OWNED"


class PackageChecksumMismatchError(MarketplaceError):
    error_code = "PACKAGE_HASH_MISMATCH"


class PackageTooLargeError(MarketplaceError):
    error_code = "PACKAGE_TOO_LARGE"


class PackageArchiveInvalidError(MarketplaceError):
    error_code = "PACKAGE_ARCHIVE_INVALID"


class PackageSecurityRejectedError(MarketplaceError):
    error_code = "PACKAGE_SECURITY_REJECTED"


class ManifestInvalidError(MarketplaceError):
    error_code = "MANIFEST_INVALID"


class InstallNotAuthorizedError(MarketplaceError):
    error_code = "INSTALL_NOT_AUTHORIZED"


class InstallTokenExpiredError(MarketplaceError):
    error_code = "INSTALL_TOKEN_EXPIRED"


class DownloadNotAuthorizedError(MarketplaceError):
    error_code = "DOWNLOAD_NOT_AUTHORIZED"


class PublishNotAllowedError(MarketplaceError):
    error_code = "PUBLISH_NOT_ALLOWED"


class SSRFBlockedError(MarketplaceError):
    error_code = "SSRF_DESTINATION_BLOCKED"
