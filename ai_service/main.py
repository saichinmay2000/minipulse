import os

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent import run_agent

load_dotenv()

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

app = FastAPI(title="MiniPulse AI Service")


class QueryRequest(BaseModel):
    messages: list[dict]
    request_id: str = ""


@app.post("/query")
async def query(req: QueryRequest) -> JSONResponse:
    log.info(
        "query_received",
        request_id=req.request_id,
        turn_count=len(req.messages),
    )
    try:
        answer = run_agent(req.messages, req.request_id)
        return JSONResponse({"answer": answer})
    except Exception as e:
        log.error("query_failed", request_id=req.request_id, error=str(e))
        return JSONResponse(
            {"answer": "I ran into an unexpected error. Please try again."},
            status_code=200,  # Always 200 to Slack adapter — errors are in the message
        )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "ai-service"}