"""One-off bootstrap:

  1. Register `jobID` and `mobile` identifier configs on the Memory Store. `jobID` is the resolution key
     (enforceUnique: true); `mobile` is the routing key (enforceUnique: false).
  2. Register the `Job` Trait Group so the app can write
     Job.status / startedAt / lastActiveAt / completedAt / conversationId / channelId
     onto profiles. Twilio Memory rejects writes to unregistered trait fields.
  3. Ensure an AI agent Memory profile exists at TWILIO_AI_NUMBER and print its ID.

Run:  python bootstrap.py
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

import twilio_helpers as tw

STORE_ID = os.environ["MEMORY_STORE_ID"]
AI_NUMBER = os.environ["TWILIO_AI_NUMBER"]


JOB_ID_IDENTIFIER: dict = {
    "idType": "jobID",
    "enforceUnique": True,
    "matchingAlgo": "exact",
    "normalization": "trim",
    "limit": 100,
    "limitPolicy": "fifo",
}

MATCHING_RULES = ["jobID", "chat", "email", "phone", "whatsapp", "pushUserID"]


JOB_TRAITS: dict[str, dict] = {
    "status": {
        "dataType": "STRING",
        "description": "Current job state: active, paused, or completed.",
    },
    "startedAt": {
        "dataType": "STRING",
        "description": "ISO 8601 timestamp when this job was first assigned.",
    },
    "lastActiveAt": {
        "dataType": "STRING",
        "description": "ISO 8601 timestamp when this job was last in the active state.",
    },
    "completedAt": {
        "dataType": "STRING",
        "description": "ISO 8601 timestamp when this job was marked completed.",
    },
    "conversationId": {
        "dataType": "STRING",
        "description": "Orchestrator conversation ID currently attached to this job.",
    },
    "channelId": {
        "dataType": "STRING",
        "description": "Channel SID shared by the tech + AI participants in this conversation.",
    },
    "mobile": {
        "dataType": "STRING",
        "description": "Mobile phone number for the technician.",
    },
}


async def configure_identity_resolution() -> None:
    """Merge `jobID` and `mobile` identifier configs into the store, and drop
    the gated `phone AND jobID` composite matching rule.

    Uses GET → merge → PUT because Twilio expects the full identifier list.
    """
    print(f"Configuring IdentityResolutionSettings on store={STORE_ID} ...")
    current = await tw.get_identity_resolution_settings(STORE_ID)
    existing = {c["idType"]: c for c in (current.get("identifierConfigs") or [])}
    existing["jobID"] = JOB_ID_IDENTIFIER
    result = await tw.put_identity_resolution_settings(
        STORE_ID,
        identifier_configs=list(existing.values()),
        matching_rules=MATCHING_RULES,
    )
    print(f"  OK. identifiers={sorted(existing.keys())} matchingRules={MATCHING_RULES}")
    return result


async def register_job_trait_group() -> None:
    print(f"Registering Trait Group 'Job' on store={STORE_ID} ...")
    try:
        result = await tw.ensure_trait_group(
            STORE_ID,
            display_name="Job",
            traits=JOB_TRAITS,
            description="Per-job state for the jobID-scoped SMS demo.",
        )
        print(f"  OK: {result}")
    except tw.TwilioError as e:
        print(f"  FAILED: {e}")
        raise


async def ensure_ai_profile() -> None:
    """Create or find the AI agent's Memory profile.

    The AI's profile is the one place `phone` is set as an identifier — it
    represents the Twilio number the AI/dispatcher speak from. Tech profiles
    do NOT populate `phone`.
    """
    print(f"\nLooking up AI profile at phone={AI_NUMBER} ...")
    candidates = await tw.lookup_profiles_by_identifier(STORE_ID, "phone", AI_NUMBER)
    if candidates:
        print(f"  Found existing profile(s): {candidates}")
        print(f"\nAI_AGENT_PROFILE_ID={candidates[0]}")
        return
    print("  None found. Creating AI agent profile ...")
    body = {
        "traits": {
            "Contact": {
                "firstName": "AI Agent",
                "chat": "0000",
                "jobID": "0000",
                "phone": AI_NUMBER,
                "mobile": AI_NUMBER,
            }
        }
    }
    resp = await tw._req(
        "POST",
        f"{tw.MEMORY_BASE}/Stores/{STORE_ID}/Profiles",
        json=body,
    )
    print(f"  Created: {resp}")
    print(f"\nAI_AGENT_PROFILE_ID={resp['id']}")
    print("\nAdd the line above to your .env file.")


async def main() -> None:
    await configure_identity_resolution()
    await register_job_trait_group()
    await ensure_ai_profile()


if __name__ == "__main__":
    asyncio.run(main())
