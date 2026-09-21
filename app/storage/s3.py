"""
Talos Cloud — S3-compatible Storage Provider (Tigris).

Wraps boto3 with asynchronous execution via asyncio.to_thread.
Works with any standard S3-compatible provider (Tigris, AWS S3, MinIO, Cloudflare R2).
Credentials and tokens are strictly kept server-side and never exposed.
"""

import asyncio
import logging
from typing import Any, BinaryIO, Optional
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.storage.base import StorageProvider
from app.storage.exceptions import (
    StorageConfigurationError,
    StorageDownloadError,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageUploadError,
)
from app.storage.models import StorageMetadata

logger = logging.getLogger("talos.storage.s3")


import concurrent.futures

_s3_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="talos_s3")


async def _run_sync(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_s3_thread_pool, func, *args)


class S3StorageProvider(StorageProvider):
    """
    S3-compatible storage provider implementation using boto3.
    Thread-safe and async-friendly with connection timeouts, adaptive retries,
    and bounded thread execution.
    """

    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        region_name: str = "auto",
        addressing_style: str = "path",
    ) -> None:
        if not endpoint_url or not access_key_id or not secret_access_key or not bucket_name:
            raise StorageConfigurationError("Missing required S3 storage configuration parameters")

        self.endpoint_url = endpoint_url
        self.bucket_name = bucket_name
        self.region_name = region_name or "auto"
        self.addressing_style = addressing_style

        # Configure botocore client with strict timeouts, adaptive retries, and S3v4
        client_config = Config(
            signature_version="s3v4",
            connect_timeout=5.0,
            read_timeout=30.0,
            s3={"addressing_style": addressing_style},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 5, "mode": "adaptive"},
        )

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=self.region_name,
            config=client_config,
        )

    async def check_health(self) -> dict[str, Any]:
        """Performs lightweight bucket health check."""
        import time
        t0 = time.perf_counter()
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                _s3_thread_pool,
                lambda: self._client.head_bucket(Bucket=self.bucket_name)
            )
            lat = round((time.perf_counter() - t0) * 1000, 2)
            return {"status": "healthy", "bucket": self.bucket_name, "latency_ms": lat}
        except Exception as e:
            lat = round((time.perf_counter() - t0) * 1000, 2)
            return {"status": "unhealthy", "bucket": self.bucket_name, "latency_ms": lat, "error": str(e)}

    def _get_bucket(self, bucket: Optional[str] = None) -> str:
        return bucket or self.bucket_name

    def _handle_client_error(self, err: ClientError, operation: str, key: str) -> None:
        error_code = err.response.get("Error", {}).get("Code", "Unknown")
        status_code = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 500)

        # Do not include raw authorization or secret headers in logs or messages
        sanitized_msg = f"S3 {operation} failed on object '{key}': {error_code}"

        if error_code in ("NoSuchKey", "NotFound") or status_code == 404:
            raise StorageNotFoundError(sanitized_msg) from err
        elif error_code in ("AccessDenied", "Forbidden") or status_code == 403:
            raise StoragePermissionError(sanitized_msg) from err
        elif operation == "upload":
            raise StorageUploadError(sanitized_msg) from err
        elif operation in ("download", "get_metadata"):
            raise StorageDownloadError(sanitized_msg) from err
        else:
            raise StorageError(sanitized_msg) from err

    async def exists(self, key: str, bucket: Optional[str] = None) -> bool:
        """Check if an object exists by calling head_object."""
        target_bucket = self._get_bucket(bucket)

        def _sync_head():
            try:
                self._client.head_object(Bucket=target_bucket, Key=key)
                return True
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")
                status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in ("NoSuchKey", "NotFound", "404") or status == 404:
                    return False
                # If access denied or other error, raise mapped exception
                self._handle_client_error(e, "head_object", key)

        return await _run_sync(_sync_head)

    async def get_metadata(self, key: str, bucket: Optional[str] = None) -> StorageMetadata:
        """Retrieve object metadata via head_object."""
        target_bucket = self._get_bucket(bucket)

        def _sync_get_meta() -> StorageMetadata:
            try:
                resp = self._client.head_object(Bucket=target_bucket, Key=key)
                last_modified = resp.get("LastModified")
                if last_modified and not last_modified.tzinfo:
                    last_modified = last_modified.replace(tzinfo=timezone.utc)

                return StorageMetadata(
                    key=key,
                    bucket=target_bucket,
                    size=resp.get("ContentLength", 0),
                    content_type=resp.get("ContentType", "application/octet-stream"),
                    etag=resp.get("ETag", "").strip('"'),
                    last_modified=last_modified,
                )
            except ClientError as e:
                self._handle_client_error(e, "get_metadata", key)

        return await _run_sync(_sync_get_meta)

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> StorageMetadata:
        """Upload in-memory bytes."""
        target_bucket = self._get_bucket(bucket)

        def _sync_put() -> StorageMetadata:
            try:
                resp = self._client.put_object(
                    Bucket=target_bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                )
                return StorageMetadata(
                    key=key,
                    bucket=target_bucket,
                    size=len(data),
                    content_type=content_type,
                    etag=resp.get("ETag", "").strip('"'),
                    last_modified=datetime.now(timezone.utc),
                )
            except ClientError as e:
                self._handle_client_error(e, "upload", key)

        return await _run_sync(_sync_put)

    async def upload_stream(
        self,
        key: str,
        source_file: BinaryIO,
        content_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> StorageMetadata:
        """Upload from an open binary stream/file directly to S3."""
        target_bucket = self._get_bucket(bucket)

        def _sync_upload_fileobj() -> StorageMetadata:
            try:
                extra_args = {"ContentType": content_type}
                self._client.upload_fileobj(
                    Fileobj=source_file,
                    Bucket=target_bucket,
                    Key=key,
                    ExtraArgs=extra_args,
                )
                head = self._client.head_object(Bucket=target_bucket, Key=key)
                return StorageMetadata(
                    key=key,
                    bucket=target_bucket,
                    size=head.get("ContentLength", 0),
                    content_type=head.get("ContentType", content_type),
                    etag=head.get("ETag", "").strip('"'),
                    last_modified=datetime.now(timezone.utc),
                )
            except ClientError as e:
                self._handle_client_error(e, "upload", key)

        return await _run_sync(_sync_upload_fileobj)

    async def download(self, key: str, bucket: Optional[str] = None) -> bytes:
        """Download raw object bytes."""
        target_bucket = self._get_bucket(bucket)

        def _sync_get() -> bytes:
            try:
                resp = self._client.get_object(Bucket=target_bucket, Key=key)
                return resp["Body"].read()
            except ClientError as e:
                self._handle_client_error(e, "download", key)

        return await _run_sync(_sync_get)

    async def delete(self, key: str, bucket: Optional[str] = None) -> bool:
        """Delete an object."""
        target_bucket = self._get_bucket(bucket)

        def _sync_delete() -> bool:
            try:
                self._client.delete_object(Bucket=target_bucket, Key=key)
                return True
            except ClientError as e:
                self._handle_client_error(e, "delete", key)

        return await _run_sync(_sync_delete)

    async def create_upload_url(
        self,
        key: str,
        expires_in: int = 900,
        content_type: Optional[str] = None,
        bucket: Optional[str] = None,
    ) -> str:
        """Generate short-lived presigned PUT URL."""
        target_bucket = self._get_bucket(bucket)

        params = {"Bucket": target_bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type

        try:
            url = self._client.generate_presigned_url(
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=expires_in,
            )
            return url
        except Exception as e:
            raise StorageUploadError(f"Failed to generate presigned upload URL: {e}") from e

    async def create_download_url(
        self,
        key: str,
        expires_in: int = 900,
        filename: Optional[str] = None,
        bucket: Optional[str] = None,
    ) -> str:
        """Generate short-lived presigned GET URL."""
        target_bucket = self._get_bucket(bucket)

        params = {"Bucket": target_bucket, "Key": key}
        if filename:
            clean_fn = filename.replace('"', "").replace("\n", "").replace("\r", "")
            params["ResponseContentDisposition"] = f'attachment; filename="{clean_fn}"'

        try:
            url = self._client.generate_presigned_url(
                ClientMethod="get_object",
                Params=params,
                ExpiresIn=expires_in,
            )
            return url
        except Exception as e:
            raise StorageDownloadError(f"Failed to generate presigned download URL: {e}") from e

    async def copy(
        self,
        source_key: str,
        dest_key: str,
        bucket: Optional[str] = None,
    ) -> None:
        """Copy object within bucket."""
        target_bucket = self._get_bucket(bucket)
        copy_source = {"Bucket": target_bucket, "Key": source_key}

        def _sync_copy():
            try:
                self._client.copy_object(
                    Bucket=target_bucket,
                    Key=dest_key,
                    CopySource=copy_source,
                )
            except ClientError as e:
                self._handle_client_error(e, "copy", dest_key)

        await _run_sync(_sync_copy)

    async def download_stream_to_file(
        self,
        key: str,
        target_file: BinaryIO,
        bucket: Optional[str] = None,
        chunk_size: int = 65536,
    ) -> int:
        """
        Streams object chunks directly to an open file without buffering the entire
        file in memory. Returns total bytes transferred.
        """
        target_bucket = self._get_bucket(bucket)

        def _sync_stream() -> int:
            try:
                resp = self._client.get_object(Bucket=target_bucket, Key=key)
                body = resp["Body"]
                total = 0
                while True:
                    chunk = body.read(chunk_size)
                    if not chunk:
                        break
                    target_file.write(chunk)
                    total += len(chunk)
                return total
            except ClientError as e:
                self._handle_client_error(e, "download_stream", key)

        return await _run_sync(_sync_stream)
