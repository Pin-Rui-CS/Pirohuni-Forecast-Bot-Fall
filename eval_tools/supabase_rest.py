"""Minimal Supabase client over plain HTTP: table upserts, reads, file uploads.

Uses httpx, which the bot already depends on, rather than the supabase SDK --
the forecast library needs three operations, not a client library.

Authentication handles both key formats Supabase issues. The current secret
keys (``sb_secret_...``) are not JWTs and must be sent on the ``apikey`` header
only; the Storage gateway rejects them as a Bearer token. Legacy service-role
keys are JWTs and also want ``Authorization: Bearer``. So ``apikey`` is always
sent, and ``Authorization`` only for a JWT-shaped key.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

import httpx

_UPSERT_BATCH = 500


class SupabaseError(RuntimeError):
    pass


class SupabaseREST:
    def __init__(
        self,
        url: str,
        key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.url = url.rstrip("/")
        headers = {"apikey": key}
        if key.startswith("eyJ"):  # legacy JWT service-role key
            headers["Authorization"] = f"Bearer {key}"
        self._client = httpx.Client(
            base_url=self.url, headers=headers, timeout=timeout, transport=transport
        )

    @classmethod
    def from_env(cls, **kwargs: Any) -> SupabaseREST | None:
        """A client from SUPABASE_URL / SUPABASE_SERVICE_KEY, or None if unset.

        None rather than an error: the publishing steps are wired into every
        workflow, and must be a silent no-op until the project is created.
        """
        url = (os.getenv("SUPABASE_URL") or "").strip()
        key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
        if not url or not key:
            return None
        return cls(url, key, **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SupabaseREST:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _check(response: httpx.Response, what: str) -> httpx.Response:
        if response.status_code >= 300:
            raise SupabaseError(
                f"{what} failed: HTTP {response.status_code}: {response.text[:500]}"
            )
        return response

    def upsert(self, table: str, rows: Iterable[dict], on_conflict: str) -> int:
        """Insert-or-update rows keyed on ``on_conflict``. Returns rows sent.

        PostgREST requires every row in one request to have the same keys;
        callers build uniform rows.
        """
        batch: list[dict] = []
        sent = 0
        for row in rows:
            batch.append(row)
            if len(batch) >= _UPSERT_BATCH:
                sent += self._upsert_batch(table, batch, on_conflict)
                batch = []
        if batch:
            sent += self._upsert_batch(table, batch, on_conflict)
        return sent

    def _upsert_batch(self, table: str, rows: list[dict], on_conflict: str) -> int:
        response = self._client.post(
            f"/rest/v1/{table}",
            params={"on_conflict": on_conflict},
            json=rows,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        self._check(response, f"upsert into {table}")
        return len(rows)

    def select(self, relation: str, params: dict[str, Any]) -> list[dict]:
        response = self._client.get(f"/rest/v1/{relation}", params=params)
        return self._check(response, f"select from {relation}").json()

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        """Write one file, replacing any existing object at ``path``."""
        response = self._client.post(
            f"/storage/v1/object/{bucket}/{path}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        self._check(response, f"upload {bucket}/{path}")
