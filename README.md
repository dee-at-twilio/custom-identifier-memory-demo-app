# jobID-Identifier SMS Demo

A three-column FastAPI app that models one field technician with one phone number and many jobs. Each job gets its own Twilio Memory profile, keyed by a custom `jobID` identifier with `enforceUnique: true`. The tech's phone number is a **trait** on the profile, not an identifier — so the app carries a tiny external index (`state.py`, a JSON file) mapping phone → `[profileId, ...]` to answer "which profiles belong to this phone?" during inbound SMS routing.

Multiple active jobs per technician are allowed. When more than one active job exists, the router requires the tech to prefix the inbound SMS with the Job ID (e.g. `4521 on site`).

## What each column does

- **Left — Technician view.** SMS thread from the tech's POV. Messages from either the AI or the dispatcher persona show inline. Includes a "Send as tech (simulated inbound)" input that runs the same routing path as a real inbound SMS.
- **Middle — Admin/AI view.** Same conversation, dispatcher perspective. Contains "Assign new job" (creates a fresh profile + conversation; does not touch existing active jobs) and "Send as dispatcher" — the human's message emits from the *same* AI Twilio number, prefixed with `[<ADMIN_PERSONA>]`. The conversation has only two participants (tech `CUSTOMER` + AI `AI_AGENT`); dispatcher messages are relayed through the AI participant with the persona prefix as the provenance marker. Twilio validates address uniqueness across participants regardless of `channelId`, so a distinct "admin" participant at the AI number isn't permitted.
- **Right — Memory profiles.** Every profile the app has recorded for the tech's phone. Badges show `active` / `paused` / `completed`. Click a card to view its conversation in the left+middle columns. Buttons: `Complete job` (active → completed, closes conversation), `Reactivate` (paused/completed → active; creates a fresh conversation if the old one is closed).

## Prerequisites

1. **A Memory Store** configured with:
   - `jobID` as a **custom identifier** with `enforceUnique: true` — the resolution key.
   - Standard identifier configs including `chat`, `email`, `phone`, `whatsapp`, `pushUserID`.
   - `matchingRules`: standard per-identifier rules only — no composite rules required.
   - A `Job` trait group registered with `status`, `startedAt`, `lastActiveAt`, `completedAt`, `conversationId`, `channelId`, `mobile`.
   The `bootstrap.py` script sets all of this up idempotently.
2. **An Orchestrator Configuration** with `GROUP_BY_PROFILE`, `memoryStoreId` pointing at that store, and — importantly — **no passive SMS `captureRules`** (see the setup step below).
3. **A Conversations classic Service** — used only to mint fresh `channelId` values for v2 conversations.
4. **One SMS-capable Twilio number** — used by both the AI agent and the human dispatcher. Its inbound webhook must point at this app. Dispatcher messages emit from the same number, prefixed with `[<ADMIN_PERSONA>]` so the technician sees a single SMS thread on their phone (SMS groups by peer number on the device — a physical constraint, not a Twilio one).
5. **OpenAI API key** for the AI replies.
6. **Python 3.10+** and **ngrok** (or any tunnel) for the inbound webhook.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in .env: Twilio SID/token, AI number, tech phone, Memory store ID,
# Orchestrator config ID, Conversations v1 service SID, OpenAI key.
```

### One-time bootstrap: identity resolution settings, `Job` trait group, AI agent profile

`bootstrap.py` performs three idempotent steps against the configured Memory store:

1. **`configure_identity_resolution`** — merges the `jobID` identifier config (`enforceUnique: true`) into the store's `IdentityResolutionSettings` alongside the standard identifiers.
2. **`register_job_trait_group`** — registers (or patches missing traits into) the custom `Job` trait group.
3. **`ensure_ai_profile`** — creates the AI agent's Memory profile at `AI_NUMBER` if one doesn't already exist. The AI profile is the **only** profile that carries the `phone` identifier.

```bash
python bootstrap.py
```

It prints something like:
```
Configuring IdentityResolutionSettings on store=mem_store_... ...
  OK

Registering Trait Group 'Job' on store=mem_store_... ...
  OK

Looking up AI profile at phone=+44... ...
  None found. Creating AI agent profile ...

AI_AGENT_PROFILE_ID=mem_profile_...
```

Paste the `AI_AGENT_PROFILE_ID=...` line into `.env`. The script is safe to re-run — every step no-ops if state already matches.

### Turn off passive SMS captureRules on the Orchestrator config

Any passive SMS capture rules on the config will auto-attach inbound SMS to a conversation via `GROUP_BY_PROFILE`. Because tech profiles don't carry the `phone` identifier — `mobile` is a trait — passive capture can't pick the right profile from an inbound SMS. This app's webhook is the routing path; clear the capture rules on the config so it takes precedence.

Two ways:
- **Console:** Conversations → Configurations → your config → Channel Settings → SMS → clear the capture rules and save.
- **API:** `GET` the config, set `channelSettings.SMS.captureRules = []`, `PUT` it back.

### Point the AI Twilio number's SMS webhook at this app

In the Console (Phone Numbers → Manage → Active numbers → your AI number → Messaging), set the "A MESSAGE COMES IN" webhook to `POST` `<your-ngrok-url>/webhooks/sms/ai`.

## Run

```bash
# terminal 1
uvicorn main:app --reload --port 8000

# terminal 2
ngrok http 8000
# copy the https URL into the AI number's inbound webhook in Console (once, or if the URL changes)
```

Then open `http://localhost:8000` in a browser.

## Try it

1. In the middle column, type a jobID (e.g. `4521`) and click **Assign new job**. The right column shows a new profile with `active` status. The middle/left columns show an empty conversation.
2. In the left column, type a simulated tech message and click **Send as tech**. It routes through the same code path as a real inbound SMS. The AI generates a reply (via OpenAI, informed by unscoped `Recall` of the profile) and Twilio sends it as a real SMS to your tech phone.
3. In the middle column, type a message and click **Send as dispatcher**. A real SMS goes to the tech's phone from the AI number, prefixed `[<ADMIN_PERSONA>] your message`. It appears in the same SMS thread the AI uses.
4. **Assign a second job** with a different jobID (e.g. `4522`). Both profiles now show `active` — multiple active jobs per tech are allowed. Each has its own conversation under a distinct `channelId`.
5. **Send an ambiguous tech message** like `on site`. The router replies with a TwiML `<Message>` listing your active job IDs and asks you to prefix the message with one. Nothing is recorded in either job's Memory profile until you pick.
6. **Send a disambiguated message** like `4522 on site`. The router strips the `4522` prefix, routes `on site` to job 4522's conversation, and the AI reply is scoped to that job's `Recall` context only.
7. **Complete one active job** with the **Complete job** button. Its conversation closes and CINTEL extraction fires. Once only one active remains, the tech no longer needs to prefix — inbound SMSes route straight through.
8. **Reactivate** a paused or completed profile — a fresh conversation is created on the same profile. Prior `ConversationSummaries` are already attached; the LLM sees the full job history via unscoped `Recall`.

## Files

- `main.py` — FastAPI app, routes, and inbound-SMS routing logic (including the multi-active jobID-prefix parse).
- `twilio_helpers.py` — thin async HTTP helpers for Memory v1 + Conversations v2 (+ v1 for `channelId` minting).
- `state.py` — JSON-file index mapping tech phone → `[profileId, ...]`. The only external state.
- `llm.py` — OpenAI reply generator with unscoped `Recall` injected into the system prompt.
- `bootstrap.py` — one-off: configures identity resolution, registers the `Job` trait group, creates the AI agent Memory profile.
- `static/index.html`, `static/app.js`, `static/style.css` — the UI.
- `summary.md` — deeper architecture reference: HTTP surface, routes, and the multi-active worked flow.
