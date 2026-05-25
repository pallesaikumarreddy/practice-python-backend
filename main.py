"""
file_service/main.py  –  FastAPI service for S3 file generation
Run locally:  uvicorn main:app --reload --port 8000
"""

import json
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="File Service",
    description="Generates a welcome file per user and uploads it to AWS S3.",
    version="1.0.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── AWS / S3 config ───────────────────────────────────────────────────────────
# Credentials are read from the environment at runtime (never hard-coded).
# In production use an IAM Role attached to the EC2/ECS task — no keys needed.
# Locally, set: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
S3_BUCKET = os.environ["S3_BUCKET_NAME"]          # required — fail fast if missing
S3_PREFIX = os.getenv("S3_PREFIX", "welcome/")    # optional folder prefix

def _s3_client():
    """
    Returns a boto3 S3 client.
    boto3 resolves credentials in this order:
      1. Environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
      2. ~/.aws/credentials
      3. IAM Role attached to the instance / task (recommended for AWS-hosted deployments)
    """
    return boto3.client(
        "s3",
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        # Explicit key injection (only needed for local dev without a profile)
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN"),  # for STS/assumed roles
    )


# ── Schemas ───────────────────────────────────────────────────────────────────
class FileRequest(BaseModel):
    user_id: int = Field(..., description="ID returned by the Java user service")
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr


class FileResponse(BaseModel):
    s3_key: str
    s3_url: str
    message: str


# ── Endpoint ──────────────────────────────────────────────────────────────────
@app.post(
    "/api/files",
    response_model=FileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a welcome file and upload to S3",
)
async def create_file(req: FileRequest) -> FileResponse:
    """
    Creates a JSON welcome file for the registered user and uploads it to the
    configured S3 bucket. Returns the S3 key and a pre-signed URL valid for
    1 hour.
    """
    # Build file content
    payload = {
        "user_id": req.user_id,
        "name": req.name,
        "email": req.email,
        "welcome_message": f"Welcome to the platform, {req.name}! 🎉",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    file_body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    # Derive S3 key: welcome/<user_id>/welcome.json
    s3_key = f"{S3_PREFIX}{req.user_id}/welcome.json"

    # Upload
    s3 = _s3_client()
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=file_body,
            ContentType="application/json",
            # Optional: server-side encryption
            ServerSideEncryption="AES256",
            # Optional: tag for lifecycle policies
            Tagging="source=registration-service",
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"S3 upload failed [{code}]: {e.response['Error']['Message']}",
        )
    except BotoCoreError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AWS error: {str(e)}",
        )

    # Generate a pre-signed URL so the caller can verify the upload
    try:
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=3600,
        )
    except ClientError:
        presigned_url = f"s3://{S3_BUCKET}/{s3_key}"

    return FileResponse(
        s3_key=s3_key,
        s3_url=presigned_url,
        message=f"Welcome file created for user {req.user_id}.",
    )


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}
