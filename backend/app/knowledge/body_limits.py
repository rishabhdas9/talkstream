from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

KNOWLEDGE_BODY_ENVELOPE_BYTES = 64 * 1024
_LIMIT_DETAIL = "Knowledge source request exceeds the configured body limit"
_SOURCE_PATHS = {"/knowledge/sources/file", "/knowledge/sources/text"}


class KnowledgeBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path) :]
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or path.rstrip("/") not in _SOURCE_PATHS
        ):
            await self.app(scope, receive, send)
            return

        lengths = [value for name, value in scope["headers"] if name.lower() == b"content-length"]
        if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
            await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                scope, receive, send
            )
            return
        if lengths:
            # Compare decimal strings to avoid integer conversion limits on untrusted headers.
            length = lengths[0].lstrip(b"0") or b"0"
            maximum = str(self.max_body_bytes).encode("ascii")
            if len(length) > len(maximum) or (len(length) == len(maximum) and length > maximum):
                await JSONResponse({"detail": _LIMIT_DETAIL}, status_code=413)(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # FastAPI preserves HTTPException; Starlette closes partial multipart files.
                    raise HTTPException(status_code=413, detail=_LIMIT_DETAIL)
            return message

        await self.app(scope, limited_receive, send)
