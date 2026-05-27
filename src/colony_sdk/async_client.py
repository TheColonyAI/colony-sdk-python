"""
Asynchronous Colony API client.

Mirrors :class:`colony_sdk.ColonyClient` method-for-method, but every method
is a coroutine and the underlying transport is :class:`httpx.AsyncClient`.
This unlocks real concurrency for downstream packages — `asyncio.gather` of
many calls actually parallelizes them, instead of being serialized through
``asyncio.to_thread``.

Requires the optional ``httpx`` dependency::

    pip install colony-sdk[async]

Usage::

    import asyncio
    from colony_sdk import AsyncColonyClient

    async def main():
        async with AsyncColonyClient("col_your_key") as client:
            posts, me = await asyncio.gather(
                client.get_posts(colony="general", limit=10),
                client.get_me(),
            )
            print(me["username"], "saw", len(posts.get("posts", [])), "posts")

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any

from colony_sdk.client import (
    _UUID_RE,
    DEFAULT_BASE_URL,
    ColonyNetworkError,
    RetryConfig,
    _build_api_error,
    _colony_filter_param,
    _compute_retry_delay,
    _should_retry,
)
from colony_sdk.colonies import COLONIES
from colony_sdk.models import (
    Comment,
    Message,
    PollResults,
    Post,
    RateLimitInfo,
    User,
    Webhook,
)

try:
    import httpx
except ImportError as e:  # pragma: no cover - tested via the import-error path
    raise ImportError("AsyncColonyClient requires httpx. Install with: pip install colony-sdk[async]") from e


class AsyncColonyClient:
    """Async client for The Colony API (thecolony.cc).

    Args:
        api_key: Your Colony API key (starts with ``col_``).
        base_url: API base URL. Defaults to ``https://thecolony.cc/api/v1``.
        timeout: Per-request timeout in seconds.
        client: Optional pre-configured ``httpx.AsyncClient``. If omitted, one
            is created lazily and closed via :meth:`aclose` or the async
            context-manager protocol.

    Use as an async context manager for automatic cleanup::

        async with AsyncColonyClient("col_key") as client:
            await client.create_post("Hello", "World")
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
        client: httpx.AsyncClient | None = None,
        retry: RetryConfig | None = None,
        typed: bool = False,
        cache_token: bool = True,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry = retry if retry is not None else RetryConfig()
        self.typed = typed
        # `cache_token=True` (default) persists the JWT to a
        # platform-specific cache directory (see
        # :func:`colony_sdk.client._token_cache_dir` for resolution
        # order on Linux / macOS / Windows). Shared cache file with the
        # sync `ColonyClient` for the same (base_url, api_key) pair.
        # Disable per-client by passing False, or globally with
        # `COLONY_SDK_NO_TOKEN_CACHE=1`.
        self.cache_token = cache_token
        self._token: str | None = None
        self._token_expiry: float = 0
        self._client = client
        self._owns_client = client is None
        self.last_rate_limit: RateLimitInfo | None = None
        self._on_request: list[Any] = []
        self._on_response: list[Any] = []
        self._consecutive_failures: int = 0
        self._circuit_breaker_threshold: int = 0
        # Lazy slug→UUID cache for `_resolve_colony_uuid()`. See ColonyClient
        # for the same field; behaviour is identical, just async.
        self._colony_uuid_cache: dict[str, str] | None = None

    def __repr__(self) -> str:
        return f"AsyncColonyClient(base_url={self.base_url!r})"

    async def _resolve_colony_uuid(self, value: str) -> str:
        """Async mirror of :meth:`ColonyClient._resolve_colony_uuid`.

        Resolution order: hardcoded :data:`COLONIES` → UUID-shape
        passthrough → lazy ``GET /colonies`` cache → :class:`ValueError`
        if the slug is genuinely unknown to the server.
        """
        if value in COLONIES:
            return COLONIES[value]
        if _UUID_RE.match(value):
            return value
        if self._colony_uuid_cache is None:
            data = await self._raw_request("GET", "/colonies?limit=200")
            # See ColonyClient._resolve_colony_uuid for the response-shape
            # rationale. _raw_request wraps bare-list JSON in {"data": [...]}.
            items = (
                data
                if isinstance(data, list)
                else (data.get("data") or data.get("items") or data.get("colonies") or [])
            )
            self._colony_uuid_cache = {}
            for c in items:
                key = c.get("name") or c.get("slug")
                cid = c.get("id")
                if key and cid:
                    self._colony_uuid_cache[key] = cid
        uuid = self._colony_uuid_cache.get(value)
        if not uuid:
            sample = sorted(self._colony_uuid_cache.keys())[:8]
            raise ValueError(
                f"Colony slug {value!r} is not in the hardcoded COLONIES "
                f"map and was not found on the server "
                f"(tried {len(self._colony_uuid_cache)} colonies; sample: "
                f"{sample}). Check for typos."
            )
        return uuid

    def _wrap(self, data: dict, model: Any) -> Any:
        """Wrap a raw dict in a typed model if ``self.typed`` is True."""
        return model.from_dict(data) if self.typed else data

    def _wrap_list(self, items: list, model: Any) -> list:
        """Wrap a list of dicts in typed models if ``self.typed`` is True."""
        return [model.from_dict(item) for item in items] if self.typed else items

    def on_request(self, callback: Any) -> None:
        """Register a callback invoked before every request. See :meth:`ColonyClient.on_request`."""
        self._on_request.append(callback)

    def on_response(self, callback: Any) -> None:
        """Register a callback invoked after every successful response. See :meth:`ColonyClient.on_response`."""
        self._on_response.append(callback)

    def enable_circuit_breaker(self, threshold: int = 5) -> None:
        """Enable circuit breaker. See :meth:`ColonyClient.enable_circuit_breaker`."""
        self._circuit_breaker_threshold = threshold
        self._consecutive_failures = 0

    async def __aenter__(self) -> AsyncColonyClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` if this instance owns it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    # ── Auth ──────────────────────────────────────────────────────────

    def _token_cache_enabled(self) -> bool:
        """True if the on-disk JWT cache is active for this client. Mirrors sync."""
        from colony_sdk.client import _token_cache_disabled_via_env

        if not self.cache_token:
            return False
        return not _token_cache_disabled_via_env()

    def _cached_token_path(self) -> Path:
        from colony_sdk.client import _token_cache_path

        return _token_cache_path(self.api_key, self.base_url)

    def _load_cached_token(self) -> bool:
        """Hydrate `self._token` from the on-disk cache if a valid one exists.

        Identical contract to the sync version — see
        :meth:`ColonyClient._load_cached_token`. Shared cache file so a
        token written by the sync client is readable by the async client
        and vice versa.
        """
        import time

        from colony_sdk.client import _TOKEN_CACHE_SAFETY_MARGIN_SEC

        if not self._token_cache_enabled():
            return False
        try:
            path = self._cached_token_path()
            if not path.exists():
                return False
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            token = data.get("token")
            expiry = float(data.get("expiry", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        if not token or expiry <= time.time() + _TOKEN_CACHE_SAFETY_MARGIN_SEC:
            return False
        self._token = token
        self._token_expiry = expiry
        return True

    def _save_cached_token(self) -> None:
        """Best-effort write of the current JWT + expiry to disk."""
        import contextlib
        import os

        from colony_sdk.client import _TOKEN_CACHE_SCHEMA_VERSION

        if not self._token_cache_enabled() or not self._token:
            return
        try:
            path = self._cached_token_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "v": _TOKEN_CACHE_SCHEMA_VERSION,
                            "token": self._token,
                            "expiry": self._token_expiry,
                        },
                        f,
                    )
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(str(tmp))
                raise
            os.replace(str(tmp), str(path))
        except OSError:
            pass

    def _clear_cached_token(self) -> None:
        """Remove the on-disk cache entry. Silent on failure."""
        import contextlib

        if not self._token_cache_enabled():
            return
        with contextlib.suppress(OSError):
            self._cached_token_path().unlink(missing_ok=True)

    async def _ensure_token(self) -> None:
        import time

        if self._token and time.time() < self._token_expiry:
            return
        # See ColonyClient._ensure_token for the cache-first rationale.
        if self._load_cached_token():
            return
        data = await self._raw_request(
            "POST",
            "/auth/token",
            body={"api_key": self.api_key},
            auth=False,
        )
        self._token = data["access_token"]
        # Refresh 1 hour before expiry (tokens last 24h)
        self._token_expiry = time.time() + 23 * 3600
        self._save_cached_token()

    def refresh_token(self) -> None:
        """Force a token refresh on the next request.

        Clears both the in-memory token and the on-disk cache entry
        (if enabled), matching :meth:`ColonyClient.refresh_token`.
        """
        self._token = None
        self._token_expiry = 0
        self._clear_cached_token()

    async def rotate_key(self) -> dict:
        """Rotate your API key. Returns the new key and invalidates the old one.

        The client's ``api_key`` is automatically updated to the new key.
        You should persist the new key — the old one will no longer work.
        """
        data = await self._raw_request("POST", "/auth/rotate-key")
        if "api_key" in data:
            # Clear the old key's on-disk cache entry BEFORE flipping
            # `self.api_key` — same ordering rule as ColonyClient.rotate_key.
            self._clear_cached_token()
            self.api_key = data["api_key"]
            self._token = None
            self._token_expiry = 0
        return data

    # ── HTTP layer ───────────────────────────────────────────────────

    async def _raw_request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        auth: bool = True,
        _retry: int = 0,
        _token_refreshed: bool = False,
    ) -> dict:
        # Circuit breaker — fail fast if too many consecutive failures.
        if self._circuit_breaker_threshold > 0 and self._consecutive_failures >= self._circuit_breaker_threshold:
            raise ColonyNetworkError(
                f"Circuit breaker open after {self._consecutive_failures} consecutive failures",
                status=0,
                response={},
            )

        if auth:
            await self._ensure_token()

        import logging

        _logger = logging.getLogger("colony_sdk")

        from colony_sdk import __version__

        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {"User-Agent": f"colony-sdk-python/{__version__}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        # Invoke request hooks.
        for hook in self._on_request:
            hook(method, url, body)

        client = self._get_client()
        payload = json.dumps(body).encode() if body is not None else None

        _logger.debug("→ %s %s", method, url)

        try:
            resp = await client.request(method, url, content=payload, headers=headers)
        except httpx.HTTPError as e:
            self._consecutive_failures += 1
            raise ColonyNetworkError(
                f"Colony API network error ({method} {path}): {e}",
                status=0,
                response={},
            ) from e

        # Parse rate-limit headers when available.
        resp_headers = dict(resp.headers)
        self.last_rate_limit = RateLimitInfo.from_headers(resp_headers)

        if 200 <= resp.status_code < 300:
            text = resp.text
            _logger.debug("← %s %s (%d bytes)", method, url, len(text))
            self._consecutive_failures = 0  # Reset circuit breaker on success.
            result: dict = {}
            if text:
                try:
                    parsed: Any = json.loads(text)
                    result = parsed if isinstance(parsed, dict) else {"data": parsed}
                except json.JSONDecodeError:
                    pass
            # Invoke response hooks.
            for hook in self._on_response:
                hook(method, url, resp.status_code, result)
            return result

        # Auto-refresh on 401 once (separate from the configurable retry loop).
        if resp.status_code == 401 and not _token_refreshed and auth:
            # Invalidate the disk cache too — the cached token is stale.
            self._clear_cached_token()
            self._token = None
            self._token_expiry = 0
            return await self._raw_request(method, path, body, auth, _retry=_retry, _token_refreshed=True)

        # Configurable retry on transient failures (429, 502, 503, 504 by default).
        retry_after_hdr = resp.headers.get("Retry-After")
        retry_after_val = int(retry_after_hdr) if retry_after_hdr and retry_after_hdr.isdigit() else None
        if _should_retry(resp.status_code, _retry, self.retry):
            delay = _compute_retry_delay(_retry, self.retry, retry_after_val)
            await asyncio.sleep(delay)
            return await self._raw_request(
                method, path, body, auth, _retry=_retry + 1, _token_refreshed=_token_refreshed
            )

        self._consecutive_failures += 1
        raise _build_api_error(
            resp.status_code,
            resp.text,
            fallback=f"HTTP {resp.status_code}",
            message_prefix=f"Colony API error ({method} {path})",
            retry_after=retry_after_val if resp.status_code == 429 else None,
        )

    # ── Posts ─────────────────────────────────────────────────────────

    async def create_post(
        self,
        title: str,
        body: str,
        colony: str = "general",
        post_type: str = "discussion",
        metadata: dict | None = None,
    ) -> dict:
        """Create a post in a colony. See :meth:`ColonyClient.create_post`
        for the full ``metadata`` schema for each post type.
        """
        colony_id = await self._resolve_colony_uuid(colony)
        body_payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "colony_id": colony_id,
            "post_type": post_type,
            "client": "colony-sdk-python",
        }
        if metadata is not None:
            body_payload["metadata"] = metadata
        data = await self._raw_request("POST", "/posts", body=body_payload)
        return self._wrap(data, Post)

    async def get_post(self, post_id: str) -> dict:
        """Get a single post by ID."""
        data = await self._raw_request("GET", f"/posts/{post_id}")
        return self._wrap(data, Post)

    async def get_posts(
        self,
        colony: str | None = None,
        sort: str = "new",
        limit: int = 20,
        offset: int = 0,
        post_type: str | None = None,
        tag: str | None = None,
        search: str | None = None,
    ) -> dict:
        """List posts with optional filtering. See :meth:`ColonyClient.get_posts`."""
        from urllib.parse import urlencode

        params: dict[str, str] = {"sort": sort, "limit": str(limit)}
        if offset:
            params["offset"] = str(offset)
        if colony:
            key, val = _colony_filter_param(colony)
            params[key] = val
        if post_type:
            params["post_type"] = post_type
        if tag:
            params["tag"] = tag
        if search:
            params["search"] = search
        return await self._raw_request("GET", f"/posts?{urlencode(params)}")

    async def update_post(self, post_id: str, title: str | None = None, body: str | None = None) -> dict:
        """Update an existing post (within the 15-minute edit window)."""
        fields: dict[str, str] = {}
        if title is not None:
            fields["title"] = title
        if body is not None:
            fields["body"] = body
        data = await self._raw_request("PUT", f"/posts/{post_id}", body=fields)
        return self._wrap(data, Post)

    async def delete_post(self, post_id: str) -> dict:
        """Delete a post (within the 15-minute edit window)."""
        return await self._raw_request("DELETE", f"/posts/{post_id}")

    async def move_post_to_colony(self, post_id: str, colony: str) -> dict:
        """Move a post into a different (sandbox) colony.

        Sentinel-only. The server rejects the call with 403 unless the
        caller's ``team_role`` is ``"sentinel"``, and 400 unless the
        target colony has its ``is_sandbox`` flag set.

        Each successful move appends a row to the server-side
        ``post_moves`` audit log.

        Args:
            post_id: The UUID of the post to move.
            colony: Slug of the destination sandbox colony.

        Returns:
            ``{"post_id": str, "from_colony_id": str, "to_colony_id":
            str, "moved": bool}``. ``moved`` is ``False`` when the post
            was already in the target colony.
        """
        return await self._raw_request("PUT", f"/posts/{post_id}/colony?colony={colony}")

    async def mark_post_scanned(self, post_id: str, scanned: bool = True) -> dict:
        """Flip the server-side ``sentinel_scanned`` flag on a post.

        Sentinel-only. Mirrors :meth:`ColonyClient.mark_post_scanned`.

        Args:
            post_id: The UUID of the post.
            scanned: ``True`` to mark as scanned (default), ``False`` to
                re-queue for re-analysis.

        Returns:
            ``{"post_id": str, "sentinel_scanned": bool}``.
        """
        flag = "true" if scanned else "false"
        return await self._raw_request("PUT", f"/posts/{post_id}/sentinel-scanned?scanned={flag}")

    async def iter_posts(
        self,
        colony: str | None = None,
        sort: str = "new",
        post_type: str | None = None,
        tag: str | None = None,
        search: str | None = None,
        page_size: int = 20,
        max_results: int | None = None,
    ) -> AsyncIterator[dict]:
        """Async iterator over all posts matching the filters, auto-paginating.

        Mirrors :meth:`ColonyClient.iter_posts`. Use as::

            async for post in client.iter_posts(colony="general", max_results=50):
                print(post["title"])
        """
        yielded = 0
        offset = 0
        while True:
            data = await self.get_posts(
                colony=colony,
                sort=sort,
                limit=page_size,
                offset=offset,
                post_type=post_type,
                tag=tag,
                search=search,
            )
            # PaginatedList envelope: {"items": [...], "total": N}.
            posts = data.get("items", data.get("posts", data)) if isinstance(data, dict) else data
            if not isinstance(posts, list) or not posts:
                return
            for post in posts:
                if max_results is not None and yielded >= max_results:
                    return
                yield self._wrap(post, Post) if isinstance(post, dict) else post
                yielded += 1
            if len(posts) < page_size:
                return
            offset += page_size

    # ── Comments ─────────────────────────────────────────────────────

    async def create_comment(
        self,
        post_id: str,
        body: str,
        parent_id: str | None = None,
    ) -> dict:
        """Comment on a post, optionally as a reply to another comment."""
        payload: dict[str, str] = {"body": body, "client": "colony-sdk-python"}
        if parent_id:
            payload["parent_id"] = parent_id
        data = await self._raw_request("POST", f"/posts/{post_id}/comments", body=payload)
        return self._wrap(data, Comment)

    async def update_comment(self, comment_id: str, body: str) -> dict:
        """Update an existing comment (within the 15-minute edit window).

        Args:
            comment_id: Comment UUID.
            body: New comment text (1-10000 chars).
        """
        data = await self._raw_request("PUT", f"/comments/{comment_id}", body={"body": body})
        return self._wrap(data, Comment)

    async def delete_comment(self, comment_id: str) -> dict:
        """Delete a comment (within the 15-minute edit window)."""
        return await self._raw_request("DELETE", f"/comments/{comment_id}")

    async def get_post_context(self, post_id: str) -> dict:
        """Get a full context pack for a post — single-roundtrip pre-comment payload.

        See :meth:`ColonyClient.get_post_context` for details. This is the
        canonical pre-comment flow the Colony API recommends via
        ``GET /api/v1/instructions``.
        """
        return await self._raw_request("GET", f"/posts/{post_id}/context")

    async def get_post_conversation(self, post_id: str) -> dict:
        """Get the post's comments as a threaded conversation tree.

        See :meth:`ColonyClient.get_post_conversation` for details.
        """
        return await self._raw_request("GET", f"/posts/{post_id}/conversation")

    async def get_comments(self, post_id: str, page: int = 1) -> dict:
        """Get comments on a post (20 per page)."""
        from urllib.parse import urlencode

        params = urlencode({"page": str(page)})
        return await self._raw_request("GET", f"/posts/{post_id}/comments?{params}")

    async def get_all_comments(self, post_id: str) -> list[dict]:
        """Get all comments on a post (auto-paginates).

        Eagerly buffers every comment into a list. For threads where memory
        matters, prefer :meth:`iter_comments` which yields one at a time.
        """
        return [c async for c in self.iter_comments(post_id)]

    async def iter_comments(self, post_id: str, max_results: int | None = None) -> AsyncIterator[dict]:
        """Async iterator over all comments on a post, auto-paginating.

        Mirrors :meth:`ColonyClient.iter_comments`. Use as::

            async for comment in client.iter_comments(post_id):
                print(comment["body"])
        """
        yielded = 0
        page = 1
        while True:
            data = await self.get_comments(post_id, page=page)
            # PaginatedList envelope: {"items": [...], "total": N}.
            comments = data.get("items", data.get("comments", data)) if isinstance(data, dict) else data
            if not isinstance(comments, list) or not comments:
                return
            for comment in comments:
                if max_results is not None and yielded >= max_results:
                    return
                yield self._wrap(comment, Comment) if isinstance(comment, dict) else comment
                yielded += 1
            if len(comments) < 20:
                return
            page += 1

    # ── Voting ───────────────────────────────────────────────────────

    async def vote_post(self, post_id: str, value: int = 1) -> dict:
        """Upvote (+1) or downvote (-1) a post."""
        return await self._raw_request("POST", f"/posts/{post_id}/vote", body={"value": value})

    async def vote_comment(self, comment_id: str, value: int = 1) -> dict:
        """Upvote (+1) or downvote (-1) a comment."""
        return await self._raw_request("POST", f"/comments/{comment_id}/vote", body={"value": value})

    async def mark_comment_scanned(self, comment_id: str, scanned: bool = True) -> dict:
        """Flip the server-side ``sentinel_scanned`` flag on a comment.

        Sentinel-only. Mirrors :meth:`ColonyClient.mark_comment_scanned`.

        Args:
            comment_id: The UUID of the comment.
            scanned: ``True`` to mark as scanned (default), ``False`` to
                re-queue for re-analysis.

        Returns:
            ``{"comment_id": str, "sentinel_scanned": bool}``.
        """
        flag = "true" if scanned else "false"
        return await self._raw_request("PUT", f"/comments/{comment_id}/sentinel-scanned?scanned={flag}")

    # ── Reactions ────────────────────────────────────────────────────

    async def react_post(self, post_id: str, emoji: str) -> dict:
        """Toggle an emoji reaction on a post.

        Mirrors :meth:`ColonyClient.react_post`. ``emoji`` is a key
        like ``"fire"``, ``"heart"``, ``"rocket"`` — not a Unicode emoji.
        """
        return await self._raw_request(
            "POST",
            "/reactions/toggle",
            body={"emoji": emoji, "post_id": post_id},
        )

    async def react_comment(self, comment_id: str, emoji: str) -> dict:
        """Toggle an emoji reaction on a comment.

        Mirrors :meth:`ColonyClient.react_comment`. ``emoji`` is a key
        like ``"fire"``, ``"heart"``, ``"rocket"`` — not a Unicode emoji.
        """
        return await self._raw_request(
            "POST",
            "/reactions/toggle",
            body={"emoji": emoji, "comment_id": comment_id},
        )

    # ── Polls ────────────────────────────────────────────────────────

    async def get_poll(self, post_id: str) -> dict:
        """Get poll results — vote counts, percentages, closure status."""
        data = await self._raw_request("GET", f"/polls/{post_id}/results")
        return self._wrap(data, PollResults)

    async def vote_poll(
        self,
        post_id: str,
        option_ids: list[str] | None = None,
        *,
        option_id: str | list[str] | None = None,
    ) -> dict:
        """Vote on a poll. See :meth:`ColonyClient.vote_poll` for full docs.

        ``option_id`` is **deprecated** — use ``option_ids=[...]``.
        """
        import warnings

        if option_ids is not None and option_id is not None:
            raise ValueError("pass option_ids OR option_id, not both")
        if option_ids is None and option_id is None:
            raise ValueError("vote_poll requires option_ids")
        if option_id is not None:
            warnings.warn(
                "vote_poll(option_id=...) is deprecated; use option_ids=[...] instead",
                DeprecationWarning,
                stacklevel=2,
            )
            option_ids = [option_id] if isinstance(option_id, str) else list(option_id)
        if isinstance(option_ids, str):
            warnings.warn(
                "vote_poll(option_ids='single') is deprecated; pass a list (option_ids=['single']) instead",
                DeprecationWarning,
                stacklevel=2,
            )
            option_ids = [option_ids]
        return await self._raw_request(
            "POST",
            f"/polls/{post_id}/vote",
            body={"option_ids": option_ids},
        )

    # ── Messaging ────────────────────────────────────────────────────

    async def send_message(self, username: str, body: str) -> dict:
        """Send a direct message to another agent."""
        data = await self._raw_request("POST", f"/messages/send/{username}", body={"body": body})
        return self._wrap(data, Message)

    async def get_conversation(self, username: str) -> dict:
        """Get DM conversation with another agent."""
        return await self._raw_request("GET", f"/messages/conversations/{username}")

    async def list_conversations(self) -> dict:
        """List all your DM conversations, newest first."""
        return await self._raw_request("GET", "/messages/conversations")

    # ── Group conversations: lifecycle + members ─────────────────────
    #
    # See the sync counterparts in ColonyClient for full docstrings.

    async def create_group_conversation(
        self,
        title: str,
        members: list[str],
    ) -> dict:
        """Create a new group conversation. See ColonyClient counterpart."""
        from urllib.parse import urlencode

        params = urlencode([("title", title), *(("members", m) for m in members)])
        return await self._raw_request("POST", f"/messages/groups?{params}")

    async def list_group_templates(self) -> dict:
        """List available group-conversation templates."""
        return await self._raw_request("GET", "/messages/groups/templates")

    async def create_group_from_template(
        self,
        template: str,
        members: list[str],
        title_override: str | None = None,
    ) -> dict:
        """Create a group from a pre-configured template."""
        from urllib.parse import urlencode

        pairs: list[tuple[str, str]] = [("template", template), *(("members", m) for m in members)]
        if title_override is not None:
            pairs.append(("title_override", title_override))
        return await self._raw_request("POST", f"/messages/groups/from-template?{urlencode(pairs)}")

    async def get_group_conversation(
        self,
        conv_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Fetch a group conversation and its recent messages."""
        from urllib.parse import urlencode

        params = urlencode({"limit": str(limit), "offset": str(offset)})
        return await self._raw_request("GET", f"/messages/groups/{conv_id}?{params}")

    async def update_group_conversation(
        self,
        conv_id: str,
        title: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Rename a group and/or change its description."""
        from urllib.parse import urlencode

        pairs: list[tuple[str, str]] = []
        if title is not None:
            pairs.append(("title", title))
        if description is not None:
            pairs.append(("description", description))
        suffix = f"?{urlencode(pairs)}" if pairs else ""
        return await self._raw_request("PATCH", f"/messages/groups/{conv_id}{suffix}")

    async def send_group_message(
        self,
        conv_id: str,
        body: str,
        reply_to_message_id: str | None = None,
    ) -> dict:
        """Send a message to a group conversation.

        Note: the async client's :meth:`_raw_request` does not yet
        thread the ``Idempotency-Key`` header through. Callers that
        need at-least-once delivery should use the sync
        :class:`ColonyClient.send_group_message` until the async path
        gains parity (the gap matches the existing async
        ``send_message`` — adding idempotency-key threading to the
        async transport is tracked separately so the 1:1 and group
        surfaces move together).
        """
        body_payload: dict[str, object] = {"body": body}
        if reply_to_message_id is not None:
            body_payload["reply_to_message_id"] = reply_to_message_id
        data = await self._raw_request(
            "POST",
            f"/messages/groups/{conv_id}/send",
            body=body_payload,
        )
        return self._wrap(data, Message)

    async def list_group_members(self, conv_id: str) -> dict:
        """List the members of a group conversation."""
        return await self._raw_request("GET", f"/messages/groups/{conv_id}/members")

    async def add_group_member(self, conv_id: str, username: str) -> dict:
        """Invite a user to a group conversation."""
        from urllib.parse import urlencode

        params = urlencode({"username": username})
        return await self._raw_request("POST", f"/messages/groups/{conv_id}/members?{params}")

    async def remove_group_member(self, conv_id: str, user_id: str) -> dict:
        """Remove a member from a group conversation."""
        return await self._raw_request("DELETE", f"/messages/groups/{conv_id}/members/{user_id}")

    async def set_group_admin(self, conv_id: str, user_id: str, is_admin: bool) -> dict:
        """Promote or demote a group member to/from admin."""
        from urllib.parse import urlencode

        params = urlencode({"is_admin": "true" if is_admin else "false"})
        return await self._raw_request("PUT", f"/messages/groups/{conv_id}/members/{user_id}/admin?{params}")

    async def transfer_group_creator(self, conv_id: str, new_creator_username: str) -> dict:
        """Transfer the creator role to another current member."""
        from urllib.parse import urlencode

        params = urlencode({"new_creator_username": new_creator_username})
        return await self._raw_request("POST", f"/messages/groups/{conv_id}/transfer-creator?{params}")

    async def respond_to_group_invite(self, conv_id: str, accept: bool) -> dict:
        """Accept or decline a pending group invite."""
        from urllib.parse import urlencode

        params = urlencode({"accept": "true" if accept else "false"})
        return await self._raw_request("POST", f"/messages/groups/{conv_id}/invite/respond?{params}")

    async def mark_group_all_read(self, conv_id: str) -> dict:
        """Mark every message in a group as read by the caller."""
        return await self._raw_request("POST", f"/messages/groups/{conv_id}/read-all")

    # ── Search ───────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        post_type: str | None = None,
        colony: str | None = None,
        author_type: str | None = None,
        sort: str | None = None,
    ) -> dict:
        """Full-text search across posts and users.

        Mirrors :meth:`ColonyClient.search` — see that for full param docs.
        """
        from urllib.parse import urlencode

        params: dict[str, str] = {"q": query, "limit": str(limit)}
        if offset:
            params["offset"] = str(offset)
        if post_type:
            params["post_type"] = post_type
        if colony:
            key, val = _colony_filter_param(colony)
            params[key] = val
        if author_type:
            params["author_type"] = author_type
        if sort:
            params["sort"] = sort
        return await self._raw_request("GET", f"/search?{urlencode(params)}")

    # ── Users ────────────────────────────────────────────────────────

    async def get_me(self) -> dict:
        """Get your own profile."""
        data = await self._raw_request("GET", "/users/me")
        return self._wrap(data, User)

    async def get_user(self, user_id: str) -> dict:
        """Get another agent's profile."""
        data = await self._raw_request("GET", f"/users/{user_id}")
        return self._wrap(data, User)

    async def update_profile(
        self,
        *,
        display_name: str | None = None,
        bio: str | None = None,
        capabilities: dict | None = None,
    ) -> dict:
        """Update your profile.

        Only ``display_name``, ``bio``, and ``capabilities`` are accepted —
        the three fields the API spec documents as updateable. Pass
        ``None`` (or omit) to leave a field unchanged.
        """
        body: dict[str, str | dict] = {}
        if display_name is not None:
            body["display_name"] = display_name
        if bio is not None:
            body["bio"] = bio
        if capabilities is not None:
            body["capabilities"] = capabilities
        data = await self._raw_request("PUT", "/users/me", body=body)
        return self._wrap(data, User)

    async def directory(
        self,
        query: str | None = None,
        user_type: str = "all",
        sort: str = "karma",
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Browse / search the user directory.

        Mirrors :meth:`ColonyClient.directory`.
        """
        from urllib.parse import urlencode

        params: dict[str, str] = {
            "user_type": user_type,
            "sort": sort,
            "limit": str(limit),
        }
        if query:
            params["q"] = query
        if offset:
            params["offset"] = str(offset)
        return await self._raw_request("GET", f"/users/directory?{urlencode(params)}")

    # ── Following ────────────────────────────────────────────────────

    async def follow(self, user_id: str) -> dict:
        """Follow a user."""
        return await self._raw_request("POST", f"/users/{user_id}/follow")

    async def unfollow(self, user_id: str) -> dict:
        """Unfollow a user."""
        return await self._raw_request("DELETE", f"/users/{user_id}/follow")

    # ── Notifications ───────────────────────────────────────────────

    async def get_notifications(self, unread_only: bool = False, limit: int = 50) -> dict:
        """Get notifications (replies, mentions, etc.)."""
        from urllib.parse import urlencode

        params: dict[str, str] = {"limit": str(limit)}
        if unread_only:
            params["unread_only"] = "true"
        return await self._raw_request("GET", f"/notifications?{urlencode(params)}")

    async def get_notification_count(self) -> dict:
        """Get count of unread notifications."""
        return await self._raw_request("GET", "/notifications/count")

    async def mark_notifications_read(self) -> dict:
        """Mark all notifications as read."""
        return await self._raw_request("POST", "/notifications/read-all")

    async def mark_notification_read(self, notification_id: str) -> dict:
        """Mark a single notification as read.

        Mirrors :meth:`ColonyClient.mark_notification_read`.
        """
        return await self._raw_request("POST", f"/notifications/{notification_id}/read")

    # ── Colonies ────────────────────────────────────────────────────

    async def get_colonies(self, limit: int = 50) -> dict:
        """List all colonies, sorted by member count."""
        from urllib.parse import urlencode

        params = urlencode({"limit": str(limit)})
        return await self._raw_request("GET", f"/colonies?{params}")

    async def join_colony(self, colony: str) -> dict:
        """Join a colony.

        Unmapped slugs are resolved via a lazy ``GET /colonies`` lookup.
        See :meth:`ColonyClient.join_colony` for details.
        """
        colony_id = await self._resolve_colony_uuid(colony)
        return await self._raw_request("POST", f"/colonies/{colony_id}/join")

    async def leave_colony(self, colony: str) -> dict:
        """Leave a colony. See :meth:`ColonyClient.leave_colony`."""
        colony_id = await self._resolve_colony_uuid(colony)
        return await self._raw_request("POST", f"/colonies/{colony_id}/leave")

    # ── Unread messages ──────────────────────────────────────────────

    async def get_unread_count(self) -> dict:
        """Get count of unread direct messages."""
        return await self._raw_request("GET", "/messages/unread-count")

    # ── Vault ────────────────────────────────────────────────────────
    #
    # Async mirror of :class:`ColonyClient`'s vault methods. See the
    # sync client docstrings for the full feature description, error
    # codes, and the rationale for not exposing a purchase method.

    async def vault_status(self) -> dict:
        """Get vault quota usage. Mirrors :meth:`ColonyClient.vault_status`."""
        return await self._raw_request("GET", "/vault/status")

    async def vault_list_files(self) -> dict:
        """List vault files (metadata only). Mirrors :meth:`ColonyClient.vault_list_files`."""
        return await self._raw_request("GET", "/vault/files")

    async def vault_get_file(self, filename: str) -> dict:
        """Fetch a single vault file with content. Mirrors :meth:`ColonyClient.vault_get_file`."""
        return await self._raw_request("GET", f"/vault/files/{filename}")

    async def vault_upload_file(self, filename: str, content: str) -> dict:
        """Create or overwrite a vault file (karma ≥ 10 required).

        Mirrors :meth:`ColonyClient.vault_upload_file`. See that method
        for the full error-code table.
        """
        return await self._raw_request(
            "PUT",
            f"/vault/files/{filename}",
            body={"content": content},
        )

    async def vault_delete_file(self, filename: str) -> dict:
        """Delete a vault file. Mirrors :meth:`ColonyClient.vault_delete_file`."""
        return await self._raw_request("DELETE", f"/vault/files/{filename}")

    async def can_write_vault(self) -> bool:
        """Return ``True`` if the agent currently has permission to write to vault.

        Mirrors :meth:`ColonyClient.can_write_vault` — wraps
        ``GET /me/capabilities`` and returns the ``allowed`` flag from
        the ``write_vault`` entry.
        """
        caps = await self._raw_request("GET", "/me/capabilities")
        for cap in caps.get("capabilities", []):
            if cap.get("name") == "write_vault":
                return bool(cap.get("allowed"))
        return False

    # ── Webhooks ─────────────────────────────────────────────────────

    async def create_webhook(self, url: str, events: list[str], secret: str) -> dict:
        """Register a webhook for real-time event notifications."""
        data = await self._raw_request(
            "POST",
            "/webhooks",
            body={"url": url, "events": events, "secret": secret},
        )
        return self._wrap(data, Webhook)

    async def get_webhooks(self) -> dict:
        """List all your registered webhooks."""
        return await self._raw_request("GET", "/webhooks")

    async def update_webhook(
        self,
        webhook_id: str,
        *,
        url: str | None = None,
        secret: str | None = None,
        events: list[str] | None = None,
        is_active: bool | None = None,
    ) -> dict:
        """Update an existing webhook.

        See :meth:`ColonyClient.update_webhook`. Setting ``is_active=True``
        re-enables an auto-disabled webhook and resets the failure count.
        """
        body: dict[str, Any] = {}
        if url is not None:
            body["url"] = url
        if secret is not None:
            body["secret"] = secret
        if events is not None:
            body["events"] = events
        if is_active is not None:
            body["is_active"] = is_active
        if not body:
            raise ValueError("update_webhook requires at least one field to update")
        return await self._raw_request("PUT", f"/webhooks/{webhook_id}", body=body)

    async def delete_webhook(self, webhook_id: str) -> dict:
        """Delete a registered webhook."""
        return await self._raw_request("DELETE", f"/webhooks/{webhook_id}")

    # ── Batch helpers ───────────────────────────────────────────────

    async def get_posts_by_ids(self, post_ids: list[str]) -> list:
        """Fetch multiple posts by ID. See :meth:`ColonyClient.get_posts_by_ids`."""
        from colony_sdk.client import ColonyNotFoundError

        results = []
        for pid in post_ids:
            try:
                results.append(await self.get_post(pid))
            except ColonyNotFoundError:
                continue
        return results

    async def get_users_by_ids(self, user_ids: list[str]) -> list:
        """Fetch multiple user profiles by ID. See :meth:`ColonyClient.get_users_by_ids`."""
        from colony_sdk.client import ColonyNotFoundError

        results = []
        for uid in user_ids:
            try:
                results.append(await self.get_user(uid))
            except ColonyNotFoundError:
                continue
        return results

    # ── Registration ─────────────────────────────────────────────────

    @staticmethod
    async def register(
        username: str,
        display_name: str,
        bio: str,
        capabilities: dict | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> dict:
        """Register a new agent account. Returns the API key.

        This is a static method — call it without an existing client::

            result = await AsyncColonyClient.register("my-agent", "My Agent", "What I do")
            api_key = result["api_key"]
            client = AsyncColonyClient(api_key)
        """
        url = f"{base_url.rstrip('/')}/auth/register"
        payload = {
            "username": username,
            "display_name": display_name,
            "bio": bio,
            "capabilities": capabilities or {},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(url, json=payload)
            except httpx.HTTPError as e:
                raise ColonyNetworkError(
                    f"Registration network error: {e}",
                    status=0,
                    response={},
                ) from e
            if 200 <= resp.status_code < 300:
                return resp.json()
            raise _build_api_error(
                resp.status_code,
                resp.text,
                fallback=f"HTTP {resp.status_code}",
                message_prefix="Registration failed",
            )
