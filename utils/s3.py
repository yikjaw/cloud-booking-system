"""
S3 utility functions for the Cloud Booking System.

Authentication note:
boto3 automatically uses the EC2 instance's attached IAM role when no
access keys are configured. Do NOT set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
anywhere in this app or in the environment — the IAM role handles it.
"""

import os
import uuid

import boto3
from botocore.exceptions import ClientError

S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# No credentials passed here on purpose — boto3 resolves them via the
# EC2 instance's IAM role automatically.
s3_client = boto3.client("s3", region_name=AWS_REGION)


def upload_file(file_obj, booking_id, original_filename):
    """Upload a file to S3 under a key namespaced by booking id. Returns the S3 object key."""
    file_key = f"bookings/{booking_id}/{uuid.uuid4()}_{original_filename}"
    try:
        s3_client.upload_fileobj(file_obj, S3_BUCKET_NAME, file_key)
    except ClientError as e:
        raise RuntimeError(f"S3 upload failed: {e}")
    return file_key


def download_file_stream(file_key):
    """Return a readable stream + content type for a given S3 object key."""
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=file_key)
    except ClientError as e:
        raise RuntimeError(f"S3 download failed: {e}")
    return obj["Body"], obj.get("ContentType", "application/octet-stream")


def delete_file(file_key):
    """Admin: remove an attachment from S3 (e.g. when its booking is deleted)."""
    try:
        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=file_key)
    except ClientError as e:
        raise RuntimeError(f"S3 delete failed: {e}")