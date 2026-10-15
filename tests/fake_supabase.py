"""In-memory stand-in for the Supabase Storage REST API, used instead of a
real project in tests. It mimics the responses images.SupabaseStorage relies
on, including Supabase's quirk of answering 400 for duplicates / missing objects.
"""
import json as jsonlib

import requests

FAKE_URL = "https://fake-project.supabase.co"
FAKE_KEY = "service-role-test-key-do-not-leak"
BUCKET = "part-images"


class FakeResponse:
    def __init__(self, status, body=None, content=b""):
        self.status_code = status
        self.content = jsonlib.dumps(body).encode() if body is not None else content

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        return jsonlib.loads(self.content)


NOT_FOUND = {"statusCode": "404", "error": "not_found", "message": "Object not found"}


class FakeSupabase:
    def __init__(self, url=FAKE_URL, key=FAKE_KEY, bucket=BUCKET, public=False):
        self.url, self.key = url, key
        self.buckets = {bucket: {"public": public, "objects": {}}} if bucket else {}
        self.calls = []
        self.fail = {}  # operation -> HTTP status, or "network"

    def objects(self, bucket=BUCKET):
        return self.buckets[bucket]["objects"]

    def request(self, method, url, headers=None, data=None, json=None, timeout=None):
        assert headers["Authorization"] == f"Bearer {self.key}" and headers["apikey"] == self.key
        assert timeout, "every Storage request must have a timeout"
        assert url.startswith(self.url + "/storage/v1/")
        path = url[len(self.url + "/storage/v1"):]
        op, args = self._route(method, path)
        self.calls.append(op)
        failure = self.fail.get(op)
        if failure == "network":
            raise requests.ConnectionError(f"connection refused (url={url})")
        if failure:
            return FakeResponse(failure, {"statusCode": str(failure), "error": "boom"})
        return getattr(self, "_" + op)(*args, headers=headers or {}, data=data, json=json)

    @staticmethod
    def _route(method, path):
        parts = path.strip("/").split("/")
        if parts[0] == "bucket":
            return ("get_bucket", (parts[1],)) if method == "GET" else ("create_bucket", ())
        if parts[:2] == ["object", "sign"]:
            if len(parts) == 3:
                return "sign_many", (parts[2],)
            return "sign", (parts[2], "/".join(parts[3:]))
        if parts[:2] == ["object", "list"]:
            return "list", (parts[2],)
        if parts[:2] == ["object", "authenticated"]:
            return "download", (parts[2], "/".join(parts[3:]))
        if method == "DELETE":
            return "delete", (parts[1],)
        return "upload", (parts[1], "/".join(parts[2:]))

    # ----------------------------------------------------------- endpoints
    def _upload(self, bucket, name, headers, data, json):
        objects = self.objects(bucket)
        if name in objects:
            return FakeResponse(400, {"statusCode": "409", "error": "Duplicate",
                                      "message": "The resource already exists"})
        objects[name] = (bytes(data), headers.get("Content-Type"))
        return FakeResponse(200, {"Key": f"{bucket}/{name}"})

    def _download(self, bucket, name, **_):
        obj = self.objects(bucket).get(name)
        return FakeResponse(200, content=obj[0]) if obj else FakeResponse(400, NOT_FOUND)

    def _delete(self, bucket, json, **_):
        removed = [self.objects(bucket).pop(n, None) and n for n in json["prefixes"]]
        return FakeResponse(200, [{"name": n} for n in removed if n])

    def _list(self, bucket, json, **_):
        names = sorted(self.objects(bucket))
        page = names[json["offset"]:json["offset"] + json["limit"]]
        return FakeResponse(200, [{"name": n, "id": f"id-{n}"} for n in page])

    def _signed(self, bucket, name):
        return f"/object/sign/{bucket}/{name}?token=tok-{name}"

    def _sign(self, bucket, name, json, **_):
        assert json["expiresIn"] > 0
        if name not in self.objects(bucket):
            return FakeResponse(400, NOT_FOUND)
        return FakeResponse(200, {"signedURL": self._signed(bucket, name)})

    def _sign_many(self, bucket, json, **_):
        return FakeResponse(200, [
            {"path": n, "signedURL": self._signed(bucket, n), "error": None}
            if n in self.objects(bucket) else {"path": n, "signedURL": None, "error": "Either the object does not exist"}
            for n in json["paths"]
        ])

    def _get_bucket(self, bucket, **_):
        if bucket not in self.buckets:
            return FakeResponse(400, {"statusCode": "404", "error": "Bucket not found"})
        return FakeResponse(200, {"id": bucket, "name": bucket, "public": self.buckets[bucket]["public"]})

    def _create_bucket(self, json, **_):
        if json["id"] in self.buckets:
            return FakeResponse(400, {"statusCode": "409", "error": "Duplicate"})
        self.buckets[json["id"]] = {"public": json["public"], "objects": {}, "config": json}
        return FakeResponse(200, {"name": json["id"]})
