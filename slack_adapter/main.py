import asyncio
import hashlib
import hmac
import os
import re
import time
import uuid

import httpx
import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import context_store

load_dotenv()

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "http://localhost:8001")

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

if not SLACK_SIGNING_SECRET:
    log.warning("startup_warning", reason="SLACK_SIGNING_SECRET environment variable is missing or empty. Slack signature verification will fail.")
if not SLACK_BOT_TOKEN:
    log.warning("startup_warning", reason="SLACK_BOT_TOKEN environment variable is missing or empty. Posting messages to Slack will fail.")

app = FastAPI(title="MiniPulse Slack Adapter")

# Deduplicate Slack event retries — store event_id -> timestamp
_seen_events: dict[str, float] = {}
DEDUP_WINDOW = 300


def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """
    Verify Slack HMAC-SHA256 signature and reject requests older than 5 minutes.
    """
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False

    if abs(time.time() - ts) > 300:
        return False

    base = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


async def _post_slack_message(channel: str, text: str, thread_ts: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": channel, "text": text, "thread_ts": thread_ts},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            log.error("slack_post_failed", error=data.get("error"))


async def _handle_mention(
    request_id: str,
    channel: str,
    thread_ts: str,
    user_text: str,
) -> None:
    """
    Process the mention in the background so we can return 200 to Slack instantly.
    This prevents Slack from retrying because we took too long.
    """
    log.info(
        "slack_mention_received",
        request_id=request_id,
        channel=channel,
        thread_ts=thread_ts,
        text_preview=user_text[:80],
    )

    context_store.append(thread_ts, "user", user_text)
    history = context_store.get(thread_ts)

    try:
        async with httpx.AsyncClient() as client:
            ai_resp = await client.post(
                f"{AI_SERVICE_URL}/query",
                json={"messages": history, "request_id": request_id},
                timeout=60,
            )
            ai_resp.raise_for_status()
            result = ai_resp.json()
            answer = result["answer"]
    except httpx.HTTPStatusError as e:
        log.error("ai_service_error", request_id=request_id, status=e.response.status_code)
        answer = "Sorry, I'm having trouble reaching the AI service right now. Please try again."
    except Exception as e:
        log.error("ai_service_unexpected_error", request_id=request_id, error=str(e))
        answer = "Something went wrong on my end. Please try again."

    context_store.append(thread_ts, "assistant", answer)
    await _post_slack_message(channel, answer, thread_ts)


@app.post("/slack/events")
async def slack_events(request: Request) -> Response:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    request_id = str(uuid.uuid4())

    if not _verify_slack_signature(body, timestamp, signature):
        log.warning("invalid_slack_signature", request_id=request_id)
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()

    # Slack URL verification challenge
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload["challenge"]})

    event = payload.get("event", {})
    event_id = payload.get("event_id", "")

    # Deduplicate — if we've already seen this event_id, drop it immediately
    now = time.time()
    if event_id and event_id in _seen_events:
        log.info("duplicate_event_dropped", event_id=event_id)
        return Response(status_code=200)

    # Mark as seen and prune old entries
    if event_id:
        _seen_events[event_id] = now
    for eid in list(_seen_events):
        if now - _seen_events[eid] > DEDUP_WINDOW:
            del _seen_events[eid]

    event_type = event.get("type")
    if event_type != "app_mention":
        return Response(status_code=200)

    # Ignore bot messages
    if event.get("bot_id"):
        return Response(status_code=200)

    channel = event["channel"]
    user_text: str = event.get("text", "")
    message_ts: str = event["ts"]
    thread_ts: str = event.get("thread_ts", message_ts)

    user_text = re.sub(r"<@[A-Z0-9]+>\s*", "", user_text).strip()

    if not user_text:
        return Response(status_code=200)

    # Fire and forget — return 200 to Slack immediately to prevent retries
    asyncio.create_task(_handle_mention(request_id, channel, thread_ts, user_text))

    return Response(status_code=200)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "slack-adapter"}