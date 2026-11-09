"""HTTP client for the simulator.

Reads are retried on transient failures (S5). Advancing the clock is NOT blindly retried (S6):
we always re-read the sim clock and only advance whatever is still missing, so a timeout after
the server already advanced can never make us skip time.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

import httpx

log = logging.getLogger(__name__)


class SimError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"sim returned {status}: {body[:300]}")
        self.status = status
        self.body = body


def _backoff(attempt: int) -> float:
    return min(2 ** (attempt - 1), 30)


class SimClient:
    def __init__(self, base_url: str, timeout: float = 300, max_attempts: int = 8):
        self.http = httpx.Client(base_url=base_url, timeout=timeout)
        self.max_attempts = max_attempts

    def _get(self, path: str, **params) -> httpx.Response:
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self.http.get(path, params=params or None)
                if r.status_code < 500:
                    if r.status_code >= 400:
                        raise SimError(r.status_code, r.text)
                    return r
                err: Exception = SimError(r.status_code, r.text)
            except httpx.TransportError as e:
                err = e
            if attempt == self.max_attempts:
                raise err
            log.warning("GET %s failed (%s), retry %d in %.0fs", path, err, attempt, _backoff(attempt))
            time.sleep(_backoff(attempt))
        raise AssertionError("unreachable")

    def state(self) -> dict:
        return self._get("/sim/state").json()

    def workflows(self) -> list[dict]:
        return self._get("/workflows").json()

    def list_chunks(self, kind: str, after: int) -> list[dict]:
        return self._get("/events/chunks", kind=kind, after=after).json()

    @contextmanager
    def stream_chunk(self, n: int) -> Iterator[Iterator[str]]:
        """Yields the chunk's NDJSON lines without loading the whole chunk into memory (S14).

        A failure mid-stream raises; the caller's transaction rolls back and the chunk is
        fetched again on the next loop iteration.
        """
        with self.http.stream("GET", f"/events/chunks/{n}") as r:
            if r.status_code != 200:
                raise SimError(r.status_code, r.read().decode(errors="replace"))
            yield (line for line in r.iter_lines() if line)

    def workflow(self, wid: str) -> dict:
        return self._get(f"/workflows/{wid}").json()

    def config(self, wid: str) -> dict:
        return self._get(f"/workflows/{wid}/config").json()

    def config_history(self, wid: str) -> list[dict]:
        return self._get(f"/workflows/{wid}/config/history").json()

    def approvals(self, workflow_id: str) -> list[dict]:
        return self._get("/approvals", workflow_id=workflow_id).json()

    def send(self, method: str, path: str, body: dict, retry: bool = True) -> tuple[int, dict]:
        """A write that carries our own proposal_id, so resending after a lost response is safe:
        the sim answers 409 duplicate_proposal_id / not_approved instead of acting twice.
        Returns (status, json) and leaves interpreting 4xx to the caller.
        retry=False for paid calls (LLM, validation): they have no id, so a resend could pay twice.
        """
        for attempt in range(1, (self.max_attempts if retry else 1) + 1):
            try:
                r = self.http.request(method, path, json=body)
                if r.status_code < 500:
                    return r.status_code, r.json()
                err: Exception = SimError(r.status_code, r.text)
            except httpx.TransportError as e:
                err = e
            if attempt == self.max_attempts or not retry:
                raise err
            log.warning("%s %s failed (%s), retry %d", method, path, err, attempt)
            time.sleep(_backoff(attempt))
        raise AssertionError("unreachable")

    def advance_to(self, target_minute: int) -> None:
        """Bring the sim clock to target_minute (or the scenario end), safe to retry (S6)."""
        for attempt in range(1, self.max_attempts + 1):
            st = self.state()
            if st["finished"] or st["sim_minute"] >= target_minute:
                return
            try:
                r = self.http.post("/sim/advance", json={"minutes": target_minute - st["sim_minute"]})
                if r.status_code < 500 and r.status_code != 409:
                    if r.status_code >= 400:
                        raise SimError(r.status_code, r.text)
                    return
                err: Exception = SimError(r.status_code, r.text)
            except httpx.TransportError as e:
                err = e
            if attempt == self.max_attempts:
                raise err
            log.warning("advance failed (%s), re-checking clock, retry %d", err, attempt)
            time.sleep(_backoff(attempt))
