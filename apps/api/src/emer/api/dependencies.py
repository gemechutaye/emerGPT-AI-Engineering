import hashlib

from emer.api.errors import problem
from emer.settings import settings
from emer.storage.database import Session
from emer.storage.models import BrowserSession, utcnow
from fastapi import Request
from sqlalchemy import select

COOKIE = "emer_session"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_session(request: Request) -> BrowserSession:
    if settings.shared_workspace:
        from emer.services.shared_workspace import current

        return await current()
    token = request.cookies.get(COOKIE)
    if token:
        async with Session() as db:
            session = await db.scalar(
                select(BrowserSession).where(BrowserSession.token_hash == token_hash(token))
            )
            if session and not session.revoked and session.expires_at > utcnow():
                return session
    raise problem(
        401, "SESSION_EXPIRED", "Your browser session has expired. Start a new session to continue."
    )
