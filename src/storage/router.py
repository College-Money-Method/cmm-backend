"""Storage API router — file upload and storage_files management."""

from __future__ import annotations

import uuid

import boto3
from fastapi import APIRouter, HTTPException, UploadFile
from sqlalchemy import select

from src.auth.deps import AdminDep
from src.config import settings
from src.db.deps import DbDep
from src.storage.models import StorageFile
from src.storage.s3_client import S3ClientDep
from src.storage.schemas import StorageFileOut

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


@router.post("/upload-image")
async def upload_image(file: UploadFile, _admin: AdminDep, s3: S3ClientDep):
    """Upload an image to S3 and return its public URL."""
    if not file.content_type or file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type. Allowed: {', '.join(ALLOWED_IMAGE_TYPES.keys())}",
        )

    ext = ALLOWED_IMAGE_TYPES[file.content_type]
    file_id = uuid.uuid4()
    s3_key = f"uploads/{file_id}.{ext}"

    data = await file.read()
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=s3_key,
        Body=data,
        ContentType=file.content_type,
    )

    url = f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
    return {"url": url}


@router.get("/files", response_model=list[StorageFileOut])
def list_storage_files(_admin: AdminDep, db: DbDep) -> list[StorageFileOut]:
    """Admin — list all tracked files in storage_files, newest first."""
    rows = db.execute(
        select(StorageFile).order_by(StorageFile.created_at.desc())
    ).scalars().all()
    return [StorageFileOut.model_validate(r) for r in rows]


@router.post("/files", response_model=StorageFileOut, status_code=201)
async def upload_standalone_file(file: UploadFile, _admin: AdminDep, db: DbDep, s3: S3ClientDep) -> StorageFileOut:
    """Admin — upload any file to S3 at uploads/{uuid}/{filename}, register in storage_files."""
    filename = file.filename or "file"
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
    mime_type = file.content_type or "application/octet-stream"
    file_id = uuid.uuid4()
    s3_key = f"uploads/{file_id}/{filename}"

    data = await file.read()
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=s3_key,
        Body=data,
        ContentType=mime_type,
    )

    s3_url = f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
    sf = StorageFile(
        s3_key=s3_key,
        s3_url=s3_url,
        original_filename=filename,
        extension=extension,
        mime_type=mime_type,
        file_size_bytes=len(data),
    )
    db.add(sf)
    db.commit()
    db.refresh(sf)
    return StorageFileOut.model_validate(sf)


@router.delete("/files/{file_id}", status_code=204)
def delete_storage_file(file_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> None:
    """Admin — delete a file from S3 and remove its storage_files record."""
    sf = db.get(StorageFile, file_id)
    if not sf:
        raise HTTPException(status_code=404, detail="File not found")

    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    try:
        s3.delete_object(Bucket=settings.s3_bucket_name, Key=sf.s3_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"S3 deletion failed: {e}")

    db.delete(sf)
    db.commit()
