"""
Cloud-Based Student Resource Sharing Platform - backend API.

Runs as a single AWS Lambda function exposed through a Lambda Function URL.
  - Metadata (title, subject, likes, downloads...) is stored in DynamoDB.
  - Files are stored in a private S3 bucket. The browser uploads/downloads
    directly to/from S3 using short-lived presigned URLs, so files never pass
    through Lambda.

Authentication: every route needs "Authorization: Bearer <Cognito access token>".
The token is validated by calling Cognito GetUser, so no extra libraries are needed.

Routes:
  GET    /resources                  list resources (?subject=...&q=...)
  POST   /resources                  create metadata + get presigned upload form
  POST   /resources/{id}/confirm     mark resource active after the S3 upload
  GET    /resources/{id}/download    get a presigned download link
  POST   /resources/{id}/like        toggle like for the signed-in user
  DELETE /resources/{id}             delete (owner only)

Environment variables:
  TABLE_NAME   DynamoDB table name     (e.g. StudentResources)
  BUCKET_NAME  S3 bucket for files     (e.g. srs-files-yourname-2026)
  MAX_FILE_MB  optional, default 10
"""

import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ["TABLE_NAME"]
BUCKET_NAME = os.environ["BUCKET_NAME"]
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_MB", "10")) * 1024 * 1024
URL_EXPIRY_SECONDS = 300

ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
    "txt", "md", "zip", "png", "jpg", "jpeg", "py", "java", "c", "cpp",
}

table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
# Regional, SigV4, virtual-hosted URLs avoid redirect/CORS problems with new buckets.
s3 = boto3.client(
    "s3",
    region_name=REGION,
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
cognito = boto3.client("cognito-idp", region_name=REGION)


# ---------------------------------------------------------------- helpers

class BadRequest(Exception):
    pass


class Unauthorized(Exception):
    pass


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Cannot serialise {type(value)}")


def respond(status, body):
    # CORS headers are added by the Lambda Function URL CORS settings.
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise BadRequest("Body must be valid JSON")
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object")
    return data


def clean_text(data, field, max_len, required=True):
    value = str(data.get(field, "")).strip()
    if required and not value:
        raise BadRequest(f"'{field}' is required")
    if len(value) > max_len:
        raise BadRequest(f"'{field}' must be at most {max_len} characters")
    return value


def authenticate(event):
    """Validate the Cognito access token and return the signed-in user."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        raise Unauthorized("Please sign in")
    try:
        result = cognito.get_user(AccessToken=token)
    except ClientError:
        raise Unauthorized("Session expired, please sign in again")
    attrs = {a["Name"]: a["Value"] for a in result["UserAttributes"]}
    email = attrs.get("email", "")
    return {
        "sub": attrs["sub"],
        "email": email,
        "name": attrs.get("name") or email.split("@")[0] or "Student",
    }


def public_view(item, user):
    hidden = {"s3Key", "ownerId", "likedBy"}
    view = {k: v for k, v in item.items() if k not in hidden}
    view["isOwner"] = item.get("ownerId") == user["sub"]
    view["liked"] = user["sub"] in item.get("likedBy", set())
    return view


def get_item(resource_id, require_active=True):
    item = table.get_item(Key={"resourceId": resource_id}).get("Item")
    if not item or (require_active and item.get("status") != "active"):
        raise NotFound("Resource not found")
    return item


# ---------------------------------------------------------------- handlers

def list_resources(params, user):
    subject = (params.get("subject") or "").strip().lower()
    query = (params.get("q") or "").strip().lower()

    items, kwargs = [], {}
    while True:  # scan is fine for a small class-sized dataset
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    results = []
    for item in items:
        if item.get("status") != "active":
            continue
        if subject and item.get("subject", "").lower() != subject:
            continue
        if query:
            haystack = " ".join(
                str(item.get(f, "")) for f in ("title", "description", "uploader", "subject")
            ).lower()
            if query not in haystack:
                continue
        results.append(public_view(item, user))

    results.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
    return respond(200, {"resources": results, "count": len(results)})


def create_resource(data, user):
    title = clean_text(data, "title", 100)
    subject = clean_text(data, "subject", 50)
    description = clean_text(data, "description", 500, required=False)
    file_name = clean_text(data, "fileName", 150)
    content_type = clean_text(data, "contentType", 100, required=False) or "application/octet-stream"

    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise BadRequest(f"File type '.{ext}' is not allowed")

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file_name)
    resource_id = str(uuid.uuid4())
    s3_key = f"uploads/{resource_id}/{safe_name}"

    table.put_item(Item={
        "resourceId": resource_id,
        "title": title,
        "subject": subject,
        "uploader": user["name"],
        "ownerId": user["sub"],
        "description": description,
        "fileName": file_name,
        "contentType": content_type,
        "s3Key": s3_key,
        "status": "pending",
        "downloads": 0,
        "likes": 0,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    })

    # Presigned POST lets S3 itself enforce the maximum file size.
    upload = s3.generate_presigned_post(
        Bucket=BUCKET_NAME,
        Key=s3_key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, MAX_FILE_BYTES],
        ],
        ExpiresIn=URL_EXPIRY_SECONDS,
    )
    return respond(201, {
        "resourceId": resource_id,
        "upload": upload,
        "maxFileBytes": MAX_FILE_BYTES,
    })


def confirm_upload(resource_id, user):
    item = get_item(resource_id, require_active=False)
    if item.get("ownerId") != user["sub"]:
        raise Forbidden("Only the uploader can confirm this upload")
    try:
        head = s3.head_object(Bucket=BUCKET_NAME, Key=item["s3Key"])
    except ClientError:
        raise BadRequest("File has not been uploaded yet")

    table.update_item(
        Key={"resourceId": resource_id},
        UpdateExpression="SET #s = :active, fileSize = :size",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":active": "active", ":size": head["ContentLength"]},
    )
    return respond(200, {"resourceId": resource_id, "status": "active"})


def download_resource(resource_id):
    item = get_item(resource_id)
    table.update_item(
        Key={"resourceId": resource_id},
        UpdateExpression="ADD downloads :one",
        ExpressionAttributeValues={":one": 1},
    )
    safe_name = item["fileName"].replace('"', "")
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": item["s3Key"],
            "ResponseContentDisposition": f'attachment; filename="{safe_name}"',
        },
        ExpiresIn=URL_EXPIRY_SECONDS,
    )
    return respond(200, {"downloadUrl": url})


def like_resource(resource_id, user):
    """Toggle: each user can like a resource once and un-like it again."""
    item = get_item(resource_id)
    uid = user["sub"]
    already = uid in item.get("likedBy", set())
    try:
        if already:
            result = table.update_item(
                Key={"resourceId": resource_id},
                UpdateExpression="DELETE likedBy :u ADD likes :minus",
                ConditionExpression="contains(likedBy, :uid)",
                ExpressionAttributeValues={":u": {uid}, ":uid": uid, ":minus": -1},
                ReturnValues="UPDATED_NEW",
            )
        else:
            result = table.update_item(
                Key={"resourceId": resource_id},
                UpdateExpression="ADD likes :one, likedBy :u",
                ConditionExpression="attribute_not_exists(likedBy) OR NOT contains(likedBy, :uid)",
                ExpressionAttributeValues={":u": {uid}, ":uid": uid, ":one": 1},
                ReturnValues="UPDATED_NEW",
            )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        item = get_item(resource_id)  # double click race: return current state
        return respond(200, {"likes": item.get("likes", 0), "liked": uid in item.get("likedBy", set())})
    return respond(200, {"likes": result["Attributes"]["likes"], "liked": not already})


def delete_resource(resource_id, user):
    item = get_item(resource_id, require_active=False)
    if item.get("ownerId") != user["sub"]:
        raise Forbidden("Only the person who uploaded this resource can delete it")

    s3.delete_object(Bucket=BUCKET_NAME, Key=item["s3Key"])
    table.delete_item(Key={"resourceId": resource_id})
    return respond(200, {"deleted": resource_id})


# ---------------------------------------------------------------- router

def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    parts = [p for p in event.get("rawPath", "/").split("/") if p]

    try:
        if method == "OPTIONS":
            return respond(200, {})

        user = authenticate(event)

        if parts == ["resources"]:
            if method == "GET":
                return list_resources(event.get("queryStringParameters") or {}, user)
            if method == "POST":
                return create_resource(parse_body(event), user)

        if len(parts) == 2 and parts[0] == "resources" and method == "DELETE":
            return delete_resource(parts[1], user)

        if len(parts) == 3 and parts[0] == "resources":
            resource_id, action = parts[1], parts[2]
            if action == "confirm" and method == "POST":
                return confirm_upload(resource_id, user)
            if action == "download" and method == "GET":
                return download_resource(resource_id)
            if action == "like" and method == "POST":
                return like_resource(resource_id, user)

        return respond(404, {"error": f"No route for {method} /{'/'.join(parts)}"})

    except BadRequest as e:
        return respond(400, {"error": str(e)})
    except Unauthorized as e:
        return respond(401, {"error": str(e)})
    except Forbidden as e:
        return respond(403, {"error": str(e)})
    except NotFound as e:
        return respond(404, {"error": str(e)})
    except Exception as e:  # logged to CloudWatch
        print(f"Unhandled error: {e!r}")
        return respond(500, {"error": "Internal server error"})
