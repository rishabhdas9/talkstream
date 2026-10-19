from __future__ import annotations

import json
from typing import Annotated

import pytest
from fastapi import Body, FastAPI, File, UploadFile
from starlette import formparsers

from app.config import Settings
from app.main import create_app


async def _request(
    app,
    chunks: list[bytes],
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    path: str = "/knowledge/sources/text",
    root_path: str = "",
    method: str = "POST",
):
    sent = []
    reads = 0

    async def receive():
        nonlocal reads
        chunk = chunks[reads]
        reads += 1
        return {"type": "http.request", "body": chunk, "more_body": reads < len(chunks)}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": root_path,
            "headers": headers or [],
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
        },
        receive,
        send,
    )
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent)
    return status, body, reads


@pytest.fixture
def bounded_app():
    from app.knowledge.body_limits import KnowledgeBodyLimitMiddleware

    app = FastAPI()
    app.state.called = False
    app.add_middleware(KnowledgeBodyLimitMiddleware, max_body_bytes=65_568)

    @app.post("/knowledge/sources/text")
    @app.post("/other")
    async def text_source(payload: Annotated[dict, Body()]):
        app.state.called = True
        return {"length": len(payload["text"])}

    @app.post("/knowledge/sources/file")
    async def file_source(file: Annotated[UploadFile, File()]):
        app.state.called = True
        return {"length": len(await file.read())}

    return app


@pytest.mark.parametrize("path", ["/knowledge/sources/file", "/knowledge/sources/text"])
async def test_declared_oversize_rejected_without_receiving_body(bounded_app, path):
    status, body, reads = await _request(
        bounded_app,
        [b"private-body-never-read"],
        headers=[(b"content-length", b"65569")],
        path=path,
    )
    assert status == 413
    assert reads == 0
    assert not bounded_app.state.called
    assert b"private-body" not in body


@pytest.mark.parametrize(
    "lengths",
    [[b"1", b"1"], [b"1", b"2"], [b"-1"], [b"+1"], [b"1, 2"], [b""], [b" 1"]],
)
async def test_invalid_or_duplicate_content_length_is_safe_400(bounded_app, lengths):
    status, _, reads = await _request(
        bounded_app, [b"never"], headers=[(b"content-length", value) for value in lengths]
    )
    assert (status, reads) == (400, 0)
    assert not bounded_app.state.called


@pytest.mark.parametrize("length", [None, b"0", b"1", "chunked"])
async def test_streamed_json_over_limit_stops_at_offending_chunk(bounded_app, length):
    headers = [(b"content-type", b"application/json")]
    if length == "chunked":
        headers.append((b"transfer-encoding", b"chunked"))
    elif length is not None:
        headers.append((b"content-length", length))
    status, _, reads = await _request(
        bounded_app, [b'{"text":"', b"x" * 65_568, b'"}'], headers=headers
    )
    assert (status, reads) == (413, 2)
    assert not bounded_app.state.called


@pytest.mark.parametrize("declared", [False, True])
async def test_exact_body_boundary_and_no_length_work(bounded_app, declared):
    body = b'{"text":"' + b"x" * (65_568 - 11) + b'"}'
    assert len(body) == 65_568
    headers = [(b"content-type", b"application/json")]
    if declared:
        headers.append((b"content-length", b"00065568"))
    status, response, reads = await _request(bounded_app, [body], headers=headers)
    assert (status, reads) == (200, 1)
    assert json.loads(response) == {"length": 65_557}
    assert bounded_app.state.called


@pytest.mark.parametrize("declared", [False, True])
async def test_multipart_abort_preserves_413_and_closes_partial_files(
    bounded_app, monkeypatch, declared
):
    opened = []
    original = formparsers.SpooledTemporaryFile

    def recording_file(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", recording_file)
    headers = [(b"content-type", b"multipart/form-data; boundary=limit-test")]
    if declared:
        headers.append((b"content-length", b"100"))
    first = (
        b"--limit-test\r\n"
        b'Content-Disposition: form-data; name="file"; filename="private.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\nsmall-prefix"
    )
    status, body, reads = await _request(
        bounded_app,
        [first, b"x" * 65_568, b"\r\n--limit-test--\r\n"],
        headers=headers,
        path="/knowledge/sources/file",
    )
    assert (status, reads) == (413, 2)
    assert not bounded_app.state.called
    assert opened and all(file.closed for file in opened)
    assert b"private.txt" not in body


async def test_multipart_exact_body_boundary_remains_valid(bounded_app):
    first = (
        b"--limit-test\r\n"
        b'Content-Disposition: form-data; name="file"; filename="example.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )
    last = b"\r\n--limit-test--\r\n"
    payload = b"x" * (65_568 - len(first) - len(last))
    status, body, reads = await _request(
        bounded_app,
        [first, payload, last],
        headers=[(b"content-type", b"multipart/form-data; boundary=limit-test")],
        path="/knowledge/sources/file",
    )
    assert (status, reads) == (200, 3)
    assert json.loads(body) == {"length": len(payload)}
    assert bounded_app.state.called


@pytest.mark.parametrize(
    ("path", "root_path"),
    [
        ("/knowledge/sources/text/", ""),
        ("/api/knowledge/sources/file", "/api"),
        ("/api/knowledge/sources/text/", "/api"),
        ("/knowledge/sources/text", "/api"),
    ],
)
async def test_limit_applies_before_trailing_slash_redirect_and_under_root_path(
    bounded_app, path, root_path
):
    status, _, reads = await _request(
        bounded_app,
        [b"never"],
        headers=[(b"content-length", b"65569")],
        path=path,
        root_path=root_path,
    )
    assert (status, reads) == (413, 0)


async def test_other_routes_and_methods_are_unchanged(bounded_app):
    body = json.dumps({"text": "x" * 65_568}).encode()
    status, _, reads = await _request(
        bounded_app,
        [body],
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        path="/other",
    )
    assert (status, reads) == (200, 1)
    status, _, reads = await _request(
        bounded_app, [b"never"], headers=[(b"content-length", b"999999")], method="GET"
    )
    assert (status, reads) == (405, 0)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("settings_limit,policy_limit", [(17, 32), (40, 32)])
async def test_create_app_installs_effective_policy_only_when_enabled(
    settings: Settings, tmp_path, enabled, settings_limit, policy_limit
):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"upload_bytes": policy_limit}))
    app = create_app(
        settings.model_copy(
            update={
                "knowledge_rag_enabled": enabled,
                "knowledge_max_upload_bytes": settings_limit,
                "policy_file": policy_file,
            }
        )
    )
    bound = min(settings_limit, policy_limit) + 65_536
    status, _, reads = await _request(
        app, [b"never"], headers=[(b"content-length", str(bound + 1).encode())]
    )
    assert (status, reads) == ((413, 0) if enabled else (404, 0))
