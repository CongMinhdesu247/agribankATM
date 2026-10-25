"""Bound request bodies before JSON and multipart parsing."""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
import uuid

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict[str, Any]]], Callable[..., Awaitable[None]]], Awaitable[None]]
logger = logging.getLogger(__name__)


class _RequestTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_upload_body_bytes: int, max_body_bytes: int = 64 * 1024) -> None:
        self.app = app
        self.max_upload_body_bytes = max_upload_body_bytes
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "").rstrip("/")
        limit = self.max_upload_body_bytes if path in {
            "/api/upload-preview", "/api/upload-and-reconcile",
        } and scope.get("method") == "POST" else self.max_body_bytes
        request_id = uuid.uuid4().hex

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                logger.warning("upload_rejected request_id=%s reason=invalid_content_length", request_id)
                await self._send_error(send, 400, "Invalid Content-Length", request_id)
                return
            if declared_size < 0:
                logger.warning("upload_rejected request_id=%s reason=invalid_content_length", request_id)
                await self._send_error(send, 400, "Invalid Content-Length", request_id)
                return
            if declared_size > limit:
                logger.warning("upload_rejected request_id=%s reason=body_limit", request_id)
                await self._send_error(send, 413, "Upload request is too large", request_id)
                return

        received_bytes = 0
        response_started = False
        exceeded_limit = False

        async def limited_receive():
            nonlocal received_bytes, exceeded_limit
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > limit:
                    exceeded_limit = True
                    raise _RequestTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            # Body parsers/middleware can catch the receive exception and turn it
            # into a generic 400. Replace that response with the correct 413.
            if exceeded_limit:
                if not response_started:
                    response_started = True
                    logger.warning("request_rejected request_id=%s reason=body_limit", request_id)
                    await self._send_error(send, 413, "Request body is too large", request_id)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestTooLarge:
            if not response_started:
                logger.warning("upload_rejected request_id=%s reason=body_limit", request_id)
                await self._send_error(send, 413, "Upload request is too large", request_id)

    @staticmethod
    async def _send_error(send, status_code: int, detail: str, request_id: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"x-request-id", request_id.encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
