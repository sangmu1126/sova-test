"""Bound request bytes before JSON parsing, including chunked requests."""

import asyncio

from starlette.responses import JSONResponse


class RequestBodyBoundary:
    def __init__(self, app, *, limit=1_048_576, seconds=15):
        self.app, self.limit, self.seconds = app, limit, seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        lengths = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"content-length"
        ]
        invalid = len(lengths) > 1 or any(
            not v or not v.isdigit() or len(v) > 12 for v in lengths
        )
        if invalid or (lengths and int(lengths[0]) > self.limit):
            return await JSONResponse(
                {"error": "request too large or invalid"}, status_code=413
            )(scope, receive, send)
        data = bytearray()
        try:
            async with asyncio.timeout(self.seconds):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    chunk = event.get("body", b"")
                    if len(data) + len(chunk) > self.limit:
                        return await JSONResponse(
                            {"error": "request too large"}, status_code=413
                        )(scope, receive, send)
                    data.extend(chunk)
                    if not event.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse(
                {"error": "request body timeout"}, status_code=408
            )(scope, receive, send)
        if lengths and len(data) != int(lengths[0]):
            return await JSONResponse(
                {"error": "request length mismatch"}, status_code=400
            )(scope, receive, send)
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
