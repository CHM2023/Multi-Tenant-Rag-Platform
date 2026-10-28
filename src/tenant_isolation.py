"""Tenant isolation on the MCP surface: the API key alone decides the tenant.

What this demonstrates:
- an ASGI middleware that authenticates every MCP request and pins it to exactly
  one tenant in a context variable, so no tool ever takes a tenant argument;
- missing, unknown and revoked keys all get the same 401, which says nothing
  about which tenants exist;
- keys are 256-bit random tokens stored only as a SHA-256 hash.

Excerpt from a private production codebase. The key lookup is stubbed and
marked "# STUB: production implementation is private", and the two 401 log
reasons are shortened to a fixed word for this excerpt.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_tenant_id")


def hash_key(key: str) -> str:
    """SHA-256 of the key, lower-case hex - what the column stores.

    Plain SHA-256 rather than a password hash on purpose: these are 256 bits of
    machine-generated randomness, not human-chosen secrets, so there is no
    dictionary to slow an attacker down with, and every request pays this cost.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: UUID
    tenant_id: str
    label: str


async def resolve_api_key(key: str) -> ApiKeyRecord | None:
    """Passed to ApiKeyMiddleware as `resolve`: the unrevoked key whose stored
    hash equals hash_key(key), or None."""
    # STUB: production implementation is private
    raise NotImplementedError


def current_tenant() -> str:
    """The tenant of the request being served.

    Set by ApiKeyMiddleware before the MCP app sees the request. Reading it
    outside a request is a programming error, not a missing value, so it raises
    rather than returning a default - a default tenant is exactly the failure
    mode this whole mechanism exists to remove.
    """
    return _current_tenant.get()


class ApiKeyMiddleware:
    """Authenticate the MCP surface and pin the request to one tenant.

    An unknown key, a revoked key and a missing key are all 401 with the same
    body. The caller learns the credential does not work, and nothing about
    which tenants exist.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        header_name: str,
        resolve: Callable[[str], Awaitable[ApiKeyRecord | None]],
    ) -> None:
        self._app = app
        self._header = header_name.lower()
        self._resolve = resolve

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        key = headers.get(self._header)

        if not key:
            await self._unauthorized(scope, receive, send, "missing")
            return

        record = await self._resolve(key)
        if record is None:
            await self._unauthorized(scope, receive, send, "rejected")
            return

        token = _current_tenant.set(record.tenant_id)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_tenant.reset(token)

    async def _unauthorized(
        self, scope: Scope, receive: Receive, send: Send, reason: str
    ) -> None:
        logger.warning("mcp request %s: api key %s", scope.get("path", ""), reason)
        response = JSONResponse(
            {
                "error": "unauthorized",
                "detail": f"Send a valid API key in the {self._header} header.",
            },
            status_code=401,
        )
        await response(scope, receive, send)
