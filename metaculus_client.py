from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime
from email.utils import parsedate_to_datetime
import json
import time

import httpx

from config import (
    AUTH_HEADERS,
    API_BASE_URL,
    DEFAULT_TOURNAMENT_ID,
    INITIAL_API_GET_RETRY_WAIT_SECONDS,
    MAX_API_GET_RETRIES,
    METACULUS_API_RATE_LIMITER,
    METACULUS_REQUEST_INTERVAL,
)


def _is_rate_limited_response(status_code: int, response_text: str) -> bool:
    if status_code == 429:
        return True
    lowered = response_text.lower()
    return (
        "rate limit" in lowered
        or "too many requests" in lowered
        or ("cloudflare" in lowered and "access denied" in lowered)
    )


def _truncate_response_text(text: str, max_len: int = 350) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _get_retry_wait_seconds(response: httpx.Response, fallback_wait_seconds: float) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return fallback_wait_seconds

    stripped = retry_after.strip()
    try:
        wait_seconds = float(stripped)
        if wait_seconds > 0:
            return wait_seconds
    except ValueError:
        pass

    try:
        retry_after_dt = parsedate_to_datetime(stripped)
        if retry_after_dt.tzinfo is None:
            retry_after_dt = retry_after_dt.replace(tzinfo=datetime.timezone.utc)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        wait_seconds = (retry_after_dt - now_utc).total_seconds()
        if wait_seconds > 0:
            return wait_seconds
    except Exception:
        pass

    return fallback_wait_seconds


def _metaculus_get_json_with_retries(
    url: str,
    *,
    params: dict | None = None,
    request_label: str,
) -> dict:
    wait_seconds = INITIAL_API_GET_RETRY_WAIT_SECONDS
    for attempt in range(1, MAX_API_GET_RETRIES + 1):
        time.sleep(METACULUS_REQUEST_INTERVAL)
        try:
            response = httpx.get(
                url,
                headers=AUTH_HEADERS,
                params=params,
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            if attempt == MAX_API_GET_RETRIES:
                raise RuntimeError(
                    f"{request_label} failed after {MAX_API_GET_RETRIES} tries for URL {url}: {exc}"
                ) from exc
            print(
                f"{request_label} network error on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}: {exc}. "
                f"Retrying in {wait_seconds:.1f}s."
            )
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue

        if response.status_code < 400:
            return response.json()

        response_text = response.text
        rate_limited = _is_rate_limited_response(response.status_code, response_text)
        if rate_limited and attempt < MAX_API_GET_RETRIES:
            retry_wait_seconds = _get_retry_wait_seconds(response, wait_seconds)
            print(
                f"{request_label} rate-limited on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}. "
                f"Status={response.status_code}. Retrying in {retry_wait_seconds:.1f}s."
            )
            time.sleep(retry_wait_seconds)
            wait_seconds *= 2
            continue

        raise RuntimeError(
            f"{request_label} failed on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}. "
            f"Status={response.status_code}. Response={_truncate_response_text(response_text)}"
        )

    raise RuntimeError(
        f"{request_label} failed after {MAX_API_GET_RETRIES} tries for URL {url}"
    )


async def _metaculus_async_get_json_with_retries(url: str, *, request_label: str) -> dict:
    wait_seconds = INITIAL_API_GET_RETRY_WAIT_SECONDS
    async with METACULUS_API_RATE_LIMITER:
        for attempt in range(1, MAX_API_GET_RETRIES + 1):
            await asyncio.sleep(METACULUS_REQUEST_INTERVAL)
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, headers=AUTH_HEADERS, timeout=30.0)
            except httpx.RequestError as exc:
                if attempt == MAX_API_GET_RETRIES:
                    raise RuntimeError(
                        f"{request_label} failed after {MAX_API_GET_RETRIES} tries for URL {url}: {exc}"
                    ) from exc
                print(
                    f"{request_label} network error on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}: {exc}. "
                    f"Retrying in {wait_seconds:.1f}s."
                )
                await asyncio.sleep(wait_seconds)
                wait_seconds *= 2
                continue

            if response.status_code < 400:
                return response.json()

            response_text = response.text
            rate_limited = _is_rate_limited_response(response.status_code, response_text)
            if rate_limited and attempt < MAX_API_GET_RETRIES:
                retry_wait_seconds = _get_retry_wait_seconds(response, wait_seconds)
                print(
                    f"{request_label} rate-limited on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}. "
                    f"Status={response.status_code}. Retrying in {retry_wait_seconds:.1f}s."
                )
                await asyncio.sleep(retry_wait_seconds)
                wait_seconds *= 2
                continue

            raise RuntimeError(
                f"{request_label} failed on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}. "
                f"Status={response.status_code}. Response={_truncate_response_text(response_text)}"
            )

    raise RuntimeError(
        f"{request_label} failed after {MAX_API_GET_RETRIES} tries for URL {url}"
    )


async def _metaculus_async_post_with_retries(
    url: str,
    *,
    json_payload: dict | list,
    request_label: str,
) -> httpx.Response:
    wait_seconds = INITIAL_API_GET_RETRY_WAIT_SECONDS
    async with METACULUS_API_RATE_LIMITER:
        for attempt in range(1, MAX_API_GET_RETRIES + 1):
            await asyncio.sleep(METACULUS_REQUEST_INTERVAL)
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        url,
                        json=json_payload,
                        headers=AUTH_HEADERS,
                        timeout=30.0,
                    )
            except httpx.RequestError as exc:
                if attempt == MAX_API_GET_RETRIES:
                    raise RuntimeError(
                        f"{request_label} failed after {MAX_API_GET_RETRIES} tries for URL {url}: {exc}"
                    ) from exc
                print(
                    f"{request_label} network error on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}: {exc}. "
                    f"Retrying in {wait_seconds:.1f}s."
                )
                await asyncio.sleep(wait_seconds)
                wait_seconds *= 2
                continue

            if response.status_code < 400:
                return response

            response_text = response.text
            rate_limited = _is_rate_limited_response(response.status_code, response_text)
            if rate_limited and attempt < MAX_API_GET_RETRIES:
                retry_wait_seconds = _get_retry_wait_seconds(response, wait_seconds)
                print(
                    f"{request_label} rate-limited on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}. "
                    f"Status={response.status_code}. Retrying in {retry_wait_seconds:.1f}s."
                )
                await asyncio.sleep(retry_wait_seconds)
                wait_seconds *= 2
                continue

            raise RuntimeError(
                f"{request_label} failed on try {attempt}/{MAX_API_GET_RETRIES} for URL {url}. "
                f"Status={response.status_code}. Response={_truncate_response_text(response_text)}"
            )

    raise RuntimeError(
        f"{request_label} failed after {MAX_API_GET_RETRIES} tries for URL {url}"
    )


async def post_question_comment(post_id: int, comment_text: str) -> None:
    await _metaculus_async_post_with_retries(
        f"{API_BASE_URL}/comments/create/",
        json_payload={
            "text": comment_text,
            "parent": None,
            "included_forecast": True,
            "is_private": True,
            "on_post": post_id,
        },
        request_label=f"post_question_comment(post_id={post_id})",
    )


async def post_question_prediction(question_id: int, forecast_payload: dict) -> None:
    url = f"{API_BASE_URL}/questions/forecast/"
    full_payload = [{"question": question_id, **forecast_payload}]
    print(f"::group::[PAYLOAD] Metaculus forecast submission for question {question_id}")
    print(json.dumps(full_payload, indent=2))
    print("::endgroup::")
    response = await _metaculus_async_post_with_retries(
        url,
        json_payload=full_payload,
        request_label=f"post_question_prediction(question_id={question_id})",
    )
    print(f"Prediction Post status code: {response.status_code}")


def create_forecast_payload(
    forecast: float | dict[str, float] | list[float],
    question_type: str,
) -> dict:
    if question_type == "binary":
        return {
            "probability_yes": forecast,
            "probability_yes_per_category": None,
            "continuous_cdf": None,
        }
    if question_type == "multiple_choice":
        return {
            "probability_yes": None,
            "probability_yes_per_category": forecast,
            "continuous_cdf": None,
        }
    return {
        "probability_yes": None,
        "probability_yes_per_category": None,
        "continuous_cdf": forecast,
    }


def list_posts_from_tournament(
    tournament_id: int | str = DEFAULT_TOURNAMENT_ID, offset: int = 0, count: int = 50
) -> dict:
    url_qparams = {
        "limit": count,
        "offset": offset,
        "order_by": "-hotness",
        "forecast_type": ",".join(["binary", "multiple_choice", "numeric", "discrete"]),
        "tournaments": [tournament_id],
        "statuses": "open",
        "include_description": "true",
    }
    url = f"{API_BASE_URL}/posts/"
    data = _metaculus_get_json_with_retries(
        url,
        params=url_qparams,
        request_label=(
            "list_posts_from_tournament"
            f"(tournament_id={tournament_id}, offset={offset}, limit={count})"
        ),
    )
    return data


@dataclass(frozen=True)
class OpenQuestion:
    """One open question from a tournament listing, with its close time."""

    question_id: int
    post_id: int
    scheduled_close_time: str | None = None


def list_open_questions(tournament_id: int | str = DEFAULT_TOURNAMENT_ID) -> list[OpenQuestion]:
    """Open questions in a tournament. The close time comes with the listing,
    so ordering a run's queue by it costs no extra Metaculus calls."""
    posts = list_posts_from_tournament(tournament_id)

    post_dict = dict()
    for post in posts["results"]:
        if question := post.get("question"):
            post_dict[post["id"]] = [question]

    open_questions: list[OpenQuestion] = []
    for post_id, questions in post_dict.items():
        for question in questions:
            if question.get("status") == "open":
                print(
                    f"ID: {question['id']}\nQ: {question['title']}\nCloses: "
                    f"{question['scheduled_close_time']}"
                )
                open_questions.append(
                    OpenQuestion(question["id"], post_id, question.get("scheduled_close_time"))
                )

    return open_questions


def get_open_question_ids_from_tournament(tournament_id: int | str = DEFAULT_TOURNAMENT_ID) -> list[tuple[int, int]]:
    return [(q.question_id, q.post_id) for q in list_open_questions(tournament_id)]


async def get_post_details(post_id: int) -> dict:
    url = f"{API_BASE_URL}/posts/{post_id}/"
    print(f"Getting details for {url}")
    details = await _metaculus_async_get_json_with_retries(
        url,
        request_label=f"get_post_details(post_id={post_id})",
    )
    return details


def post_tournaments(post_details: dict) -> list[dict]:
    """The tournaments a post belongs to, as ``[{id, slug, name}]``.

    Read from ``projects.tournament`` in a ``/posts/<id>/`` response, e.g.
    ``[{"id": 33108, "slug": "metaculus-cup-fall-2026", "name": "Metaculus Cup
    Fall 2026"}]``. Recorded in forecast.json so the forecast library can filter
    by competition without asking Metaculus again. Empty when the post lists
    none or the shape is unexpected -- this must never fail a forecast.
    """
    projects = post_details.get("projects") if isinstance(post_details, dict) else None
    entries = projects.get("tournament") if isinstance(projects, dict) else None
    if not isinstance(entries, list):
        return []
    return [
        {"id": entry.get("id"), "slug": entry.get("slug"), "name": entry.get("name")}
        for entry in entries
        if isinstance(entry, dict) and (entry.get("slug") or entry.get("name"))
    ]
