"""
HubSpot tool functions for MiniPulse.

Rules:
- Every function is a pure function of its inputs (HubSpot client passed in).
- No global state, no hidden clients.
- Return dicts — never raise to callers; surface errors in the returned dict.
- Never log contact email or full name at INFO level (privacy).
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

HUBSPOT_BASE = "https://api.hubapi.com"

# Map display names -> HubSpot internal stage IDs
# HubSpot stores stages as internal IDs, not display names.
# These are the custom stages we created + HubSpot defaults.
STAGE_NAME_MAP = {
    "new business": "3781445312",
    "qualified": "3781445313",
    "negotiation": "3781445314",
    "closed won": "closedwon",
    "closedwon": "closedwon",
    "closed lost": "closedlost",
    "closedlost": "closedlost",
}


def _normalize_stage(stage_name: str) -> str:
    """
    Normalize stage name to whatever HubSpot actually stored.
    Try exact match first, then lowercase lookup, then pass through as-is.
    """
    lower = stage_name.lower().strip()
    return STAGE_NAME_MAP.get(lower, stage_name)


def _make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=HUBSPOT_BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Tool 1: count_deals_by_stage
# ---------------------------------------------------------------------------

def count_deals_by_stage(stage_name: str, token: str) -> dict[str, Any]:
    """
    Return the count and total value of deals in a given pipeline stage.
    """
    tool = "count_deals_by_stage"
    start = _now_ms()
    normalized = _normalize_stage(stage_name)
    try:
        with _make_client(token) as client:
            resp = client.post(
                "/crm/v3/objects/deals/search",
                json={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "dealstage",
                                    "operator": "EQ",
                                    "value": normalized,
                                }
                            ]
                        }
                    ],
                    "properties": ["dealname", "amount", "dealstage"],
                    "limit": 100,
                },
            )
            _handle_hubspot_error(resp, tool)
            data = resp.json()

        deals = data.get("results", [])
        total_value = sum(
            float(d["properties"].get("amount") or 0) for d in deals
        )
        log.info(
            tool,
            duration_ms=_elapsed(start),
            stage=stage_name,
            count=len(deals),
            success=True,
        )
        return {
            "stage": stage_name,
            "normalized_stage": normalized,
            "count": len(deals),
            "total_value": round(total_value, 2),
            "deals": [
                {
                    "name": d["properties"].get("dealname"),
                    "amount": float(d["properties"].get("amount") or 0),
                }
                for d in deals
            ],
        }
    except _HubSpotError as e:
        return _error_result(tool, start, str(e))
    except Exception as e:
        return _error_result(tool, start, f"unexpected: {e}")


# ---------------------------------------------------------------------------
# Tool 2: summarize_closed_deals
# ---------------------------------------------------------------------------

def summarize_closed_deals(days: int = 30, token: str = "") -> dict[str, Any]:
    """
    Return count and total value of Closed Won deals in the last N days.
    """
    tool = "summarize_closed_deals"
    start = _now_ms()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_ts = int(cutoff.timestamp() * 1000)  # HubSpot uses ms epoch

        with _make_client(token) as client:
            resp = client.post(
                "/crm/v3/objects/deals/search",
                json={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "dealstage",
                                    "operator": "EQ",
                                    "value": "closedwon",
                                },
                                {
                                    "propertyName": "closedate",
                                    "operator": "GTE",
                                    "value": str(cutoff_ts),
                                },
                            ]
                        }
                    ],
                    "properties": ["dealname", "amount", "closedate", "hubspot_owner_id"],
                    "limit": 100,
                },
            )
            _handle_hubspot_error(resp, tool)
            data = resp.json()

        deals = data.get("results", [])
        total_value = sum(
            float(d["properties"].get("amount") or 0) for d in deals
        )
        log.info(tool, duration_ms=_elapsed(start), days=days, count=len(deals), success=True)
        return {
            "days": days,
            "count": len(deals),
            "total_value": round(total_value, 2),
            "deals": [
                {
                    "name": d["properties"].get("dealname"),
                    "amount": float(d["properties"].get("amount") or 0),
                    "close_date": d["properties"].get("closedate"),
                }
                for d in deals
            ],
        }
    except _HubSpotError as e:
        return _error_result(tool, start, str(e))
    except Exception as e:
        return _error_result(tool, start, f"unexpected: {e}")


# ---------------------------------------------------------------------------
# Tool 3: get_contact_by_email
# ---------------------------------------------------------------------------

def get_contact_by_email(email: str, token: str) -> dict[str, Any]:
    """
    Look up a contact by email. Returns name, role, and associated company.
    Does NOT log the email or full name at INFO level.
    """
    tool = "get_contact_by_email"
    start = _now_ms()
    try:
        with _make_client(token) as client:
            resp = client.post(
                "/crm/v3/objects/contacts/search",
                json={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "email",
                                    "operator": "EQ",
                                    "value": email,
                                }
                            ]
                        }
                    ],
                    "properties": ["firstname", "lastname", "jobtitle", "company"],
                    "limit": 1,
                },
            )
            _handle_hubspot_error(resp, tool)
            data = resp.json()

        results = data.get("results", [])
        log.info(tool, duration_ms=_elapsed(start), found=len(results) > 0, success=True)

        if not results:
            return {"found": False, "message": f"No contact found with that email."}

        props = results[0]["properties"]
        return {
            "found": True,
            "first_name": props.get("firstname"),
            "last_name": props.get("lastname"),
            "job_title": props.get("jobtitle"),
            "company": props.get("company"),
        }
    except _HubSpotError as e:
        return _error_result(tool, start, str(e))
    except Exception as e:
        return _error_result(tool, start, f"unexpected: {e}")


# ---------------------------------------------------------------------------
# Tool 4: search_deals
# ---------------------------------------------------------------------------

def search_deals(
    min_amount: float = 0,
    stage_name: str = "",
    owner_name: str = "",
    limit: int = 10,
    token: str = "",
) -> dict[str, Any]:
    """
    Flexible deal search by amount threshold, stage, and/or owner name.
    Returns matching deals sorted by amount descending.
    """
    tool = "search_deals"
    start = _now_ms()
    try:
        filters = []

        if min_amount > 0:
            filters.append({
                "propertyName": "amount",
                "operator": "GTE",
                "value": str(min_amount),
            })

        if stage_name:
            filters.append({
                "propertyName": "dealstage",
                "operator": "EQ",
                "value": _normalize_stage(stage_name),
            })

        search_body: dict[str, Any] = {
            "filterGroups": [{"filters": filters}] if filters else [{"filters": []}],
            "properties": ["dealname", "amount", "dealstage", "closedate", "hubspot_owner_id"],
            "sorts": [{"propertyName": "amount", "direction": "DESCENDING"}],
            "limit": min(limit, 50),
        }

        with _make_client(token) as client:
            resp = client.post("/crm/v3/objects/deals/search", json=search_body)
            _handle_hubspot_error(resp, tool)
            data = resp.json()

        deals = data.get("results", [])

        # Client-side owner filter (HubSpot owner search requires owner ID lookup)
        if owner_name:
            owner_lower = owner_name.lower()
            deals = [
                d for d in deals
                if owner_lower in (d["properties"].get("hubspot_owner_id") or "").lower()
            ]

        log.info(tool, duration_ms=_elapsed(start), count=len(deals), success=True)
        return {
            "count": len(deals),
            "deals": [
                {
                    "name": d["properties"].get("dealname"),
                    "amount": float(d["properties"].get("amount") or 0),
                    "stage": d["properties"].get("dealstage"),
                    "close_date": d["properties"].get("closedate"),
                }
                for d in deals
            ],
        }
    except _HubSpotError as e:
        return _error_result(tool, start, str(e))
    except Exception as e:
        return _error_result(tool, start, f"unexpected: {e}")


# ---------------------------------------------------------------------------
# Tool 5: get_deal_owner
# ---------------------------------------------------------------------------

def get_deal_owner(deal_name: str, token: str) -> dict[str, Any]:
    """
    Find who owns a deal by its name. Returns owner details.
    """
    tool = "get_deal_owner"
    start = _now_ms()
    try:
        with _make_client(token) as client:
            # Search by deal name
            resp = client.post(
                "/crm/v3/objects/deals/search",
                json={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "dealname",
                                    "operator": "CONTAINS_TOKEN",
                                    "value": deal_name,
                                }
                            ]
                        }
                    ],
                    "properties": ["dealname", "amount", "hubspot_owner_id"],
                    "limit": 5,
                },
            )
            _handle_hubspot_error(resp, tool)
            data = resp.json()
            results = data.get("results", [])

            if not results:
                log.info(tool, duration_ms=_elapsed(start), found=False, success=True)
                return {"found": False, "message": f"No deal found matching '{deal_name}'."}

            deal = results[0]
            owner_id = deal["properties"].get("hubspot_owner_id")
            owner_info = {}

            if owner_id:
                owner_resp = client.get(f"/crm/v3/owners/{owner_id}")
                if owner_resp.status_code == 200:
                    o = owner_resp.json()
                    owner_info = {
                        "first_name": o.get("firstName"),
                        "last_name": o.get("lastName"),
                        "email": o.get("email"),
                    }

        log.info(tool, duration_ms=_elapsed(start), found=True, success=True)
        return {
            "found": True,
            "deal_name": deal["properties"].get("dealname"),
            "amount": float(deal["properties"].get("amount") or 0),
            "owner": owner_info,
        }
    except _HubSpotError as e:
        return _error_result(tool, start, str(e))
    except Exception as e:
        return _error_result(tool, start, f"unexpected: {e}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _HubSpotError(Exception):
    pass


def _handle_hubspot_error(resp: httpx.Response, tool: str) -> None:
    if resp.status_code == 429:
        log.warning(f"{tool}_rate_limited", status=429)
        raise _HubSpotError("rate_limited")
    if resp.status_code >= 500:
        log.error(f"{tool}_hubspot_5xx", status=resp.status_code)
        raise _HubSpotError(f"hubspot_server_error:{resp.status_code}")
    if resp.status_code >= 400:
        log.error(f"{tool}_hubspot_4xx", status=resp.status_code, body=resp.text[:200])
        raise _HubSpotError(f"hubspot_client_error:{resp.status_code}")


def _error_result(tool: str, start: float, reason: str) -> dict[str, Any]:
    log.error(tool, duration_ms=_elapsed(start), error=reason, success=False)
    if "rate_limited" in reason:
        return {"error": "rate_limited", "message": "HubSpot rate limit hit. Please try again shortly."}
    return {"error": "hubspot_unavailable", "message": "I couldn't reach HubSpot right now. Please try again."}


def _now_ms() -> float:
    import time
    return time.time() * 1000


def _elapsed(start: float) -> int:
    import time
    return int(time.time() * 1000 - start)