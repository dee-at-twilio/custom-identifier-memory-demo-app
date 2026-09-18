"""Thin HTTP helpers for Twilio Memory (v1) and Conversations (v2).

Identifier model (no gated composite feature key):
  * `chat`   — set to jobID on each tech-job profile (standard identifier).
  * `jobID`  — custom identifier, also set to jobID (enforceUnique: true).
    This is the resolution key: POSTing a profile whose Contact.jobID
    already exists resolves to that same profile.

The tech's phone number lives as a **trait** (`Job.mobile`), not an
identifier. Consequently Memory can't answer "which profiles belong to this
phone?" — the app maintains that mapping locally in `state.py`.

The `phone` identifier is used only by the AI agent's profile (bootstrapped
at TWILIO_AI_NUMBER). It is deliberately NOT populated on tech-job profiles.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

MEMORY_BASE = "https://memory.twilio.com/v1"
CONV_V2_BASE = "https://conversations.twilio.com/v2"
CONV_V1_BASE = "https://conversations.twilio.com/v1"


def _auth() -> tuple[str, str]:
    return (os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class TwilioError(Exception):
    def __init__(self, resp: httpx.Response) -> None:
        super().__init__(f"{resp.request.method} {resp.request.url} -> {resp.status_code}: {resp.text}")
        self.status_code = resp.status_code
        self.body = resp.text


async def _req(method: str, url: str, **kwargs: Any) -> dict:
    async with httpx.AsyncClient(auth=_auth(), timeout=30.0) as client:
        resp = await client.request(method, url, **kwargs)
    if resp.status_code >= 400:
        raise TwilioError(resp)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


# ---------- Memory: identity resolution settings ----------

async def get_identity_resolution_settings(store_id: str) -> dict:
    return await _req(
        "GET",
        f"{MEMORY_BASE}/ControlPlane/Stores/{store_id}/IdentityResolutionSettings",
    )


async def put_identity_resolution_settings(
    store_id: str,
    identifier_configs: list[dict],
    matching_rules: list[str],
) -> dict:
    """Overwrite the store's IdentityResolutionSettings.

    Twilio requires all identifier configs to be sent together — GET, merge,
    then PUT.
    """
    return await _req(
        "PUT",
        f"{MEMORY_BASE}/ControlPlane/Stores/{store_id}/IdentityResolutionSettings",
        json={
            "identifierConfigs": identifier_configs,
            "matchingRules": matching_rules,
        },
    )


# ---------- Memory: trait groups ----------

async def create_trait_group(
    store_id: str,
    display_name: str,
    traits: dict[str, dict],
    description: str = "",
) -> dict:
    """Register a Trait Group. Each entry in `traits` is {dataType: STRING|NUMBER|BOOLEAN|ARRAY, description?}."""
    body = {"displayName": display_name, "description": description, "traits": traits}
    return await _req(
        "POST",
        f"{MEMORY_BASE}/ControlPlane/Stores/{store_id}/TraitGroups",
        json=body,
    )


async def get_trait_group(store_id: str, group_name: str) -> dict | None:
    try:
        return await _req(
            "GET",
            f"{MEMORY_BASE}/ControlPlane/Stores/{store_id}/TraitGroups/{group_name}",
            params={"includeTraits": "true"},
        )
    except TwilioError as e:
        if e.status_code == 404:
            return None
        raise


async def patch_trait_group(
    store_id: str,
    group_name: str,
    traits: dict[str, dict],
    description: str | None = None,
) -> dict:
    body: dict[str, Any] = {"traits": traits}
    if description is not None:
        body["description"] = description
    return await _req(
        "PATCH",
        f"{MEMORY_BASE}/ControlPlane/Stores/{store_id}/TraitGroups/{group_name}",
        json=body,
    )


async def ensure_trait_group(
    store_id: str,
    display_name: str,
    traits: dict[str, dict],
    description: str = "",
) -> dict:
    """Idempotent: create the group if missing, otherwise merge missing traits into it."""
    existing = await get_trait_group(store_id, display_name)
    if existing is None:
        return await create_trait_group(store_id, display_name, traits, description)
    # Merge in any traits that aren't already registered.
    got = ((existing.get("traitGroup") or {}).get("traits")) or {}
    missing = {name: spec for name, spec in traits.items() if name not in got}
    if missing:
        return await patch_trait_group(store_id, display_name, missing)
    return existing


# ---------- Memory: profiles ----------

async def create_or_resolve_profile(
    store_id: str,
    tech_phone: str,
    job_id: str,
    first_name: str | None = None,
) -> dict:
    """Create a tech-job profile, or resolve to the existing one via jobID.

    Contact identifiers set on the profile:
      * `chat`   = job_id
      * `jobID`  = job_id  (enforceUnique: true — resolution key)
    Contact traits: `firstName` (if provided).
    Job traits: `status`, `startedAt`, `lastActiveAt`, `mobile` (tech's phone
    number — a trait, not an identifier).

    POSTing the same jobID returns the existing profile.
    """
    contact: dict[str, Any] = {
        # "chat": job_id,
        "jobID": job_id,
    }
    if first_name:
        contact["firstName"] = first_name
    body = {
        "traits": {
            "Contact": contact,
            "Job": {
                "status": "active",
                "startedAt": _now_iso(),
                "lastActiveAt": _now_iso(),
                "mobile": tech_phone,
            },
        }
    }
    return await _req("POST", f"{MEMORY_BASE}/Stores/{store_id}/Profiles", json=body)


async def get_profile(store_id: str, profile_id: str) -> dict:
    return await _req("GET", f"{MEMORY_BASE}/Stores/{store_id}/Profiles/{profile_id}")


async def patch_profile_traits(store_id: str, profile_id: str, traits: dict) -> dict:
    """Merge trait groups into a profile without disturbing others."""
    return await _req(
        "PATCH",
        f"{MEMORY_BASE}/Stores/{store_id}/Profiles/{profile_id}",
        json={"traits": traits},
    )


async def delete_profile(store_id: str, profile_id: str) -> dict:
    """Delete a profile. Irreversible — Memory does not soft-delete."""
    return await _req(
        "DELETE",
        f"{MEMORY_BASE}/Stores/{store_id}/Profiles/{profile_id}",
    )


async def lookup_profiles_by_identifier(store_id: str, id_type: str, value: str) -> list[str]:
    """Return every profile id whose identifier of `id_type` matches `value`."""
    resp = await _req(
        "POST",
        f"{MEMORY_BASE}/Stores/{store_id}/Profiles/Lookup",
        json={"idType": id_type, "value": value},
    )
    return resp.get("profiles", []) or []


async def resolve_by_job_id(store_id: str, job_id: str) -> str | None:
    """Return the profile id whose `jobID` identifier matches, else None."""
    ids = await lookup_profiles_by_identifier(store_id, "jobID", job_id)
    return ids[0] if ids else None


async def find_active_profiles_among(store_id: str, profile_ids: list[str]) -> list[dict]:
    """Every profile in `profile_ids` whose `Job.status == 'active'`.

    Returns full profile objects so callers can read traits without a second
    GET. Profile ids that no longer exist in Memory are skipped. The tech may
    hold multiple active jobs concurrently — inbound-SMS routing uses the
    length of this list to decide whether to auto-route or ask the tech to
    disambiguate with a job ID prefix.
    """
    out = []
    for pid in profile_ids:
        try:
            prof = await get_profile(store_id, pid)
        except TwilioError as e:
            if e.status_code == 404:
                continue
            raise
        job = (prof.get("traits") or {}).get("Job") or {}
        if job.get("status") == "active":
            out.append(prof)
    return out


async def list_profiles(store_id: str, profile_ids: list[str]) -> list[dict]:
    """Full profile records for every id in `profile_ids` that still exists."""
    out = []
    for pid in profile_ids:
        try:
            prof = await get_profile(store_id, pid)
        except TwilioError as e:
            if e.status_code == 404:
                continue
            raise
        out.append(prof)
    return out


async def recall(
    store_id: str,
    profile_id: str,
    conversation_id: str | None = None,
    observations_limit: int = 10,
    summaries_limit: int = 3,
) -> dict:
    body: dict[str, Any] = {
        "observationsLimit": observations_limit,
        "summariesLimit": summaries_limit,
    }
    if conversation_id:
        body["conversationId"] = conversation_id
    return await _req(
        "POST",
        f"{MEMORY_BASE}/Stores/{store_id}/Profiles/{profile_id}/Recall",
        json=body,
    )


async def post_observation(
    store_id: str,
    profile_id: str,
    content: str,
    source: str,
    occurred_at: str,
    conversation_id: str | None = None,
) -> dict:
    obs: dict[str, Any] = {
        "content": content,
        "source": source,
        "occurredAt": occurred_at,
    }
    if conversation_id:
        obs["conversationIds"] = [conversation_id]
    return await _req(
        "POST",
        f"{MEMORY_BASE}/Stores/{store_id}/Profiles/{profile_id}/Observations",
        json={"observations": [obs]},
    )


async def post_conversation_summary(
    store_id: str,
    profile_id: str,
    conversation_id: str,
    content: str,
    source: str,
    occurred_at: str,
) -> dict:
    return await _req(
        "POST",
        f"{MEMORY_BASE}/Stores/{store_id}/Profiles/{profile_id}/ConversationSummaries",
        json={
            "summaries": [
                {
                    "conversationId": conversation_id,
                    "content": content,
                    "source": source,
                    "occurredAt": occurred_at,
                }
            ]
        },
    )


# ---------- Conversations classic (v1): channelId minting ----------

async def mint_channel_id(service_sid: str) -> str:
    """Create a classic Conversation just to harvest its CH- SID for use as a unique channelId."""
    resp = await _req("POST", f"{CONV_V1_BASE}/Services/{service_sid}/Conversations")
    return resp["sid"]


# ---------- Conversations v2: create + read + close ----------

async def create_orchestrator_conversation(
    config_id: str,
    customer_profile_id: str,
    ai_agent_profile_id: str,
    tech_phone: str,
    ai_number: str,
    channel_id: str,
) -> dict:
    """Create an Orchestrator conversation with two participants: tech (CUSTOMER)
    and AI (AI_AGENT). Both share the same channelId.

    Human dispatcher messages are relayed through the AI participant (with a
    persona prefix on the body). Twilio validates that participant addresses are
    unique per conversation regardless of channelId, so a separate 'admin'
    participant at the AI number is not permissible; provenance for dispatcher
    messages is carried by the content prefix instead.
    """
    payload = {
        "configurationId": config_id,
        "participants": [
            {
                "type": "CUSTOMER",
                "profileId": customer_profile_id,
                "addresses": [
                    {"address": tech_phone, "channel": "SMS", "channelId": channel_id}
                ],
            },
            {
                "type": "AI_AGENT",
                "profileId": ai_agent_profile_id,
                "addresses": [
                    {"address": ai_number, "channel": "SMS", "channelId": channel_id}
                ],
            },
        ],
    }
    return await _req("POST", f"{CONV_V2_BASE}/Conversations", json=payload)


async def get_conversation(conversation_id: str) -> dict:
    return await _req("GET", f"{CONV_V2_BASE}/Conversations/{conversation_id}")


async def list_communications(conversation_id: str, page_size: int = 100) -> list[dict]:
    resp = await _req(
        "GET",
        f"{CONV_V2_BASE}/Conversations/{conversation_id}/Communications",
        params={"PageSize": page_size},
    )
    return resp.get("communications", []) or []


async def set_conversation_state(conversation_id: str, state: str) -> dict:
    """Set Orchestrator conversation state. state ∈ {ACTIVE, INACTIVE, CLOSED}. Uses PUT."""
    if state not in ("ACTIVE", "INACTIVE", "CLOSED"):
        raise ValueError(f"invalid conversation state: {state}")
    return await _req(
        "PUT",
        f"{CONV_V2_BASE}/Conversations/{conversation_id}",
        json={"status": state},
    )


async def close_conversation(conversation_id: str) -> dict:
    return await set_conversation_state(conversation_id, "CLOSED")


# ---------- Conversations v2: send + record ----------

async def send_message_action(
    conversation_id: str,
    sender_participant_id: str,
    recipient_participant_id: str,
    from_address: str,
    to_address: str,
    body: str,
) -> dict:
    """Ask Twilio to emit an outbound SMS. Only works when the 'from' is a Twilio number."""
    payload = {
        "type": "SEND_MESSAGE",
        "payload": {
            "to": [
                {
                    "channel": "SMS",
                    "participantId": recipient_participant_id,
                    "address": to_address,
                }
            ],
            "from": {
                "channel": "SMS",
                "participantId": sender_participant_id,
                "address": from_address,
            },
            "content": {"text": body},
        },
    }
    return await _req(
        "POST",
        f"{CONV_V2_BASE}/Conversations/{conversation_id}/Actions",
        json=payload,
    )


async def record_communication(
    conversation_id: str,
    author_participant_id: str,
    author_address: str,
    recipient_participant_id: str,
    recipient_address: str,
    text: str,
    channel_id: str | None = None,
    resource_id: str | None = None,
) -> dict:
    """Record that a message occurred. Does NOT emit an SMS.

    `resource_id` should be the Twilio Message SID (`SMxxxx`) for outbound
    messages sent via `send_message_action` — read it from
    `action["related"]["messageSid"]`. Setting it cross-links the
    Communication with the underlying SMS resource so status callbacks and
    the Console show one unified thread.
    """
    payload: dict[str, Any] = {
        "author": {
            "address": author_address,
            "channel": "SMS",
            "participantId": author_participant_id,
        },
        "content": {"type": "TEXT", "text": text},
        "recipients": [
            {
                "address": recipient_address,
                "channel": "SMS",
                "participantId": recipient_participant_id,
            }
        ],
    }
    if channel_id:
        payload["channelId"] = channel_id
    if resource_id:
        payload["resourceId"] = resource_id
    return await _req(
        "POST",
        f"{CONV_V2_BASE}/Conversations/{conversation_id}/Communications",
        json=payload,
    )


# ---------- Convenience: participants ----------

def find_participant(conversation: dict, address: str | None = None, ptype: str | None = None) -> dict | None:
    for p in conversation.get("participants", []) or []:
        if ptype and p.get("type") != ptype:
            continue
        if address is None:
            return p
        for addr in p.get("addresses", []) or []:
            if addr.get("address") == address:
                return p
    return None
