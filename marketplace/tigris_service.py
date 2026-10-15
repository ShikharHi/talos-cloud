import os
import boto3
from botocore.config import Config
from typing import Optional, BinaryIO
from config import config

class TigrisStorageService:
    def __init__(self):
        self.endpoint = config.TIGRIS_ENDPOINT
        self.bucket = config.TIGRIS_BUCKET
        self.access_key = config.TIGRIS_ACCESS_KEY
        self.secret_key = config.TIGRIS_SECRET_KEY
        
        self._s3_client = None

    def _get_client(self):
        if self._s3_client is None:
            self._s3_client = boto3.client(
                's3',
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                config=Config(signature_version='s3v4')
            )
        return self._s3_client

    def upload_file(self, local_path: str, object_key: str, content_type: str = "application/zip") -> str:
        """Upload a local file to Tigris object storage and return tigris uri."""
        client = self._get_client()
        with open(local_path, "rb") as f:
            client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=f,
                ContentType=content_type
            )
        return f"tigris://{self.bucket}/{object_key}"

    def upload_bytes(self, file_bytes: bytes, object_key: str, content_type: str = "application/zip") -> str:
        """Upload raw bytes to Tigris object storage and return tigris uri."""
        client = self._get_client()
        client.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=file_bytes,
            ContentType=content_type
        )
        return f"tigris://{self.bucket}/{object_key}"

    def generate_download_url(self, object_key: str, expires_in: int = 3600) -> str:
        """Generate a presigned download URL for a file stored in Tigris."""
        client = self._get_client()
        # If passed as tigris://bucket/key, strip prefix
        if object_key.startswith("tigris://"):
            parts = object_key.replace("tigris://", "").split("/", 1)
            if len(parts) == 2:
                object_key = parts[1]
        
        url = client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': object_key},
            ExpiresIn=expires_in
        )
        return url

    def get_object_bytes(self, object_key: str) -> Optional[bytes]:
        """Download raw object bytes from Tigris."""
        try:
            client = self._get_client()
            if object_key.startswith("tigris://"):
                parts = object_key.replace("tigris://", "").split("/", 1)
                if len(parts) == 2:
                    object_key = parts[1]
            resp = client.get_object(Bucket=self.bucket, Key=object_key)
            return resp["Body"].read()
        except Exception as e:
            print(f"[Tigris] Error getting object {object_key}: {e}")
            return None

    def delete_object(self, object_key: str) -> bool:
        """Delete an object from Tigris."""
        try:
            client = self._get_client()
            if object_key.startswith("tigris://"):
                parts = object_key.replace("tigris://", "").split("/", 1)
                if len(parts) == 2:
                    object_key = parts[1]
            client.delete_object(Bucket=self.bucket, Key=object_key)
            return True
        except Exception as e:
            print(f"[Tigris] Error deleting object {object_key}: {e}")
            return False

    def list_objects(self, prefix: str = ""):
        """List objects in Tigris bucket."""
        try:
            client = self._get_client()
            resp = client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
            return resp.get("Contents", [])
        except Exception as e:
            print(f"[Tigris] Error listing objects: {e}")
            return []

tigris_service = TigrisStorageService()
