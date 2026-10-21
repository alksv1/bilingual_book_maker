"""Static browser API calls must match the real FastAPI routing/schema surface.

No Node/browser installation or live server is needed. This intentionally reads
shipped JavaScript, rather than repeating an imaginary frontend in Python.
"""

from pathlib import Path
import re

from server.main import app

JS = Path(__file__).resolve().parents[1] / "server/static/js"


def normalize(path):
    """JS interpolation and FastAPI parameters have the same structural shape."""
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]+\}", "{}", path)
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/")


def frontend_calls(source):
    calls = []
    # request() calls in api.js have a literal/template URL and, optionally,
    # an options object. Read the entire statement to include its HTTP method.
    for match in re.finditer(
        r"\brequest\((?P<quote>['`])(?P<url>/api/.*?)(?P=quote)(?P<options>.*?)\);",
        source,
        re.S,
    ):
        method = re.search(r"\bmethod\s*:\s*['\"]([A-Z]+)['\"]", match["options"])
        calls.append((method[1] if method else "GET", normalize(match["url"])))
    # These deliberately bypass fetch(): anchors and EventSource perform GETs.
    for match in re.finditer(r"\breturn\s+`(/api/[^`]+)`\s*;", source):
        calls.append(("GET", normalize(match[1])))
    return calls


def resolve(schema, value):
    while "$ref" in value:
        current = schema
        for key in value["$ref"].removeprefix("#/").split("/"):
            current = current[key]
        value = current
    return value


def test_javascript_routes_and_methods_match_actual_app():
    source = (JS / "api.js").read_text(encoding="utf-8")
    calls = frontend_calls(source)
    schema = app.openapi()
    actual = {
        (method.upper(), normalize(path))
        for path, operations in schema["paths"].items()
        if path.startswith(("/api/auth/", "/api/tasks", "/api/settings/"))
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert calls, "No frontend API calls were extracted"
    assert len(calls) == len(set(calls)), "Duplicate/misparsed frontend endpoint"
    assert set(calls) == actual, {
        "frontend_without_route": set(calls) - actual,
        "backend_without_frontend": actual - set(calls),
    }
    assert ("GET", "/api/tasks/{}/logs/stream") in calls
    assert ("GET", "/api/tasks/{}/download") in calls
    # Pin the real consumers too: helper strings alone do not prove a browser
    # stream/download is wired to them.
    board = (JS / "components/task-board.js").read_text(encoding="utf-8")
    assert "api.tasks.getDownloadUrl(task.id)" in board
    assert "new EventSource(url)" in (JS / "sse.js").read_text(encoding="utf-8")
    stream_source = (JS / "sse.js").read_text(encoding="utf-8")
    stream_url = re.search(r"const url = `([^`]+)`", stream_source)[1]
    assert ("GET", normalize(stream_url)) in actual
    assert "new LogStreamer(task.id," in (JS / "components/log-modal.js").read_text(
        encoding="utf-8"
    )
    assert "options.credentials = 'include'" in source


def test_frontend_multipart_and_query_names_match_openapi():
    source = (JS / "api.js").read_text(encoding="utf-8")
    schema = app.openapi()
    create = schema["paths"]["/api/tasks"]["post"]
    body = resolve(
        schema, create["requestBody"]["content"]["multipart/form-data"]["schema"]
    )
    fields = set(re.findall(r"formData\.append\(['\"]([^'\"]+)['\"]", source))
    assert fields == set(body["properties"])
    assert "files" in body["required"]
    assert body["properties"]["files"]["type"] == "array"
    file_schema = body["properties"]["files"]["items"]
    assert (
        file_schema.get("format") == "binary"
        or file_schema.get("contentMediaType") == "application/octet-stream"
    )
    upload = re.search(r"async create\(.*?\n    \},", source, re.S)[0]
    assert "for (const file of files)" in upload
    assert "body: formData" in upload
    assert "Content-Type" not in upload, "Browser must supply multipart boundary"

    params = re.search(r"new URLSearchParams\(\{(.*?)\}\)", source, re.S)[1]
    names = set(re.findall(r"^\s*(\w+)\s*:", params, re.M))
    list_params = schema["paths"]["/api/tasks"]["get"]["parameters"]
    assert names == {
        parameter["name"] for parameter in list_params if parameter["in"] == "query"
    }
    assert "?${params.toString()}" in source
    deletion = re.search(r"async delete\(.*?\n    \},", source, re.S)[0]
    query_names = set(re.findall(r"[?&]([a-z_]+)=", deletion))
    delete_params = schema["paths"]["/api/tasks/{task_id}"]["delete"]["parameters"]
    assert query_names == {
        parameter["name"] for parameter in delete_params if parameter["in"] == "query"
    }
    assert "deleteFile = true" in deletion
    assert (
        next(p for p in delete_params if p["name"] == "delete_file")["schema"][
            "default"
        ]
        is True
    )

    stream_source = (JS / "sse.js").read_text(encoding="utf-8")
    stream_url = re.search(r"const url = `([^`]+)`", stream_source)[1]
    stream_names = set(re.findall(r"[?&]([a-z_]+)=", stream_url))
    stream_params = schema["paths"]["/api/tasks/{task_id}/logs/stream"]["get"][
        "parameters"
    ]
    assert stream_names == {
        parameter["name"] for parameter in stream_params if parameter["in"] == "query"
    }
