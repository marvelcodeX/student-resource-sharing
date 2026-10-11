"""Offline tests for the Lambda backend. AWS (S3, DynamoDB) is simulated with moto and
Cognito's GetUser is replaced by a fake, so no AWS account or network is needed.

Run:  pip install -r requirements-dev.txt && pytest -q
"""
import importlib
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
import requests
from botocore.exceptions import ClientError
from moto import mock_aws

REGION = "ap-south-1"
USERS = {
    "tokA": {"sub": "a1", "email": "alice@example.com", "name": "Alice"},
    "tokB": {"sub": "b2", "email": "bob@example.com", "name": "Bob"},
}


def event(method, path, body=None, qs=None, token=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": qs,
        "headers": {"Authorization": f"Bearer {token}"} if token else {},
    }


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("TABLE_NAME", "StudentResources")
    monkeypatch.setenv("BUCKET_NAME", "srs-files-test")
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket="srs-files-test", CreateBucketConfiguration={"LocationConstraint": REGION})
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="StudentResources",
            KeySchema=[{"AttributeName": "resourceId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "resourceId", "AttributeType": "S"}],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5})

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        sys.modules.pop("lambda_function", None)
        module = importlib.import_module("lambda_function")

        def fake_get_user(AccessToken):
            user = USERS.get(AccessToken)
            if not user:
                raise ClientError({"Error": {"Code": "NotAuthorizedException", "Message": "bad"}}, "GetUser")
            return {"UserAttributes": [{"Name": k, "Value": v} for k, v in user.items()]}

        monkeypatch.setattr(module.cognito, "get_user", fake_get_user)

        def call(*args, **kwargs):
            result = module.lambda_handler(event(*args, **kwargs), None)
            return result["statusCode"], json.loads(result["body"])

        yield call


def upload(api, token="tokA", file_name="notes.pdf"):
    status, created = api("POST", "/resources", {
        "title": "Unit 1", "subject": "Cloud", "fileName": file_name,
        "contentType": "application/pdf"}, token=token)
    assert status == 201
    form = created["upload"]
    assert requests.post(form["url"], data=form["fields"],
                         files={"file": (file_name, b"%PDF-test")}).status_code == 204
    assert api("POST", f"/resources/{created['resourceId']}/confirm", token=token)[0] == 200
    return created["resourceId"]


def test_requires_login(api):
    assert api("GET", "/resources")[0] == 401
    assert api("GET", "/resources", token="bogus")[0] == 401


def test_cors_preflight_needs_no_login(api):
    assert api("OPTIONS", "/resources")[0] == 200


def test_rejects_disallowed_file_type(api):
    status, body = api("POST", "/resources",
                       {"title": "t", "subject": "s", "fileName": "virus.exe"}, token="tokA")
    assert status == 400 and "not allowed" in body["error"]


def test_pending_resources_are_hidden_until_confirmed(api):
    api("POST", "/resources", {"title": "t", "subject": "s", "fileName": "a.pdf"}, token="tokA")
    assert api("GET", "/resources", token="tokA")[1]["count"] == 0


def test_list_search_and_ownership_flags(api):
    upload(api)
    mine = api("GET", "/resources", qs={"q": "unit"}, token="tokA")[1]["resources"][0]
    other = api("GET", "/resources", token="tokB")[1]["resources"][0]
    assert mine["uploader"] == "Alice" and mine["isOwner"] is True
    assert other["isOwner"] is False
    assert "ownerId" not in mine and "s3Key" not in mine and "likedBy" not in mine
    assert api("GET", "/resources", qs={"q": "nomatch"}, token="tokA")[1]["count"] == 0


def test_only_owner_can_confirm_or_delete(api):
    status, created = api("POST", "/resources",
                          {"title": "t", "subject": "s", "fileName": "a.pdf"}, token="tokA")
    rid = created["resourceId"]
    assert api("POST", f"/resources/{rid}/confirm", token="tokB")[0] == 403
    rid = upload(api)
    assert api("DELETE", f"/resources/{rid}", token="tokB")[0] == 403
    assert api("DELETE", f"/resources/{rid}", token="tokA")[0] == 200
    assert api("GET", "/resources", token="tokA")[1]["count"] == 0


def test_like_toggles_once_per_user(api):
    rid = upload(api)
    assert api("POST", f"/resources/{rid}/like", token="tokB")[1] == {"likes": 1, "liked": True}
    assert api("POST", f"/resources/{rid}/like", token="tokA")[1] == {"likes": 2, "liked": True}
    assert api("POST", f"/resources/{rid}/like", token="tokB")[1] == {"likes": 1, "liked": False}


def test_download_counts_and_returns_url(api):
    rid = upload(api)
    status, body = api("GET", f"/resources/{rid}/download", token="tokB")
    assert status == 200 and body["downloadUrl"].startswith("https://")
    assert api("GET", "/resources", token="tokA")[1]["resources"][0]["downloads"] == 1


def test_unknown_route_is_404(api):
    assert api("GET", "/nope", token="tokA")[0] == 404
