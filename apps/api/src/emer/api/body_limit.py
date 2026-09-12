"""Bound request bodies even when a client sends chunked transfer without Content-Length."""

from starlette.exceptions import HTTPException


class BodyLimitMiddleware:
    def __init__(self, app, limit=100000):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.limit:
                    raise HTTPException(
                        413,
                        detail={
                            "code": "BODY_TOO_LARGE",
                            "message": "The request is too large.",
                            "retryable": False,
                        },
                    )
            return message

        await self.app(scope, limited_receive, send)
