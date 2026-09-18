# jobID-Identifier SMS Demo — API & Runtime Summary

A FastAPI three-column UI that models one field technician's phone number resolving to many distinct Memory profiles — one per job — The custom `jobID` identifier (with `enforceUnique: true`) is the resolution key; the tech's phone number lives as a trait, not an identifier. Because Memory can't answer "which profiles belong to this phone?" on its own, the app carries a **tiny external index** (`state.py`, a JSON file) mapping phone → `[profileId, ...]`. Everything else — status, timestamps, conversation wiring, observations, summaries — stays in Twilio Memory.

---

## Environment / prerequisites

- **Memory Store** with:
  - `jobID` custom identifier, `enforceUnique: true` — the resolution key
  - Standard identifier config includes `chat`, `email`, `phone`, `whatsapp`, `pushUserID` (see `bootstrap.py`)
  - `matchingRules`: standard per-identifier rules only 
  - `Job` trait group registered with `status`, `startedAt`, `lastActiveAt`, `completedAt`, `conversationId`, `channelId`, `mobile`
- **Orchestrator Configuration**:
  - `GROUP_BY_PROFILE`
  - `memoryStoreId` → the store above
  - **SMS `captureRules` cleared** — the phone number's own webhook is the router; passive capture can't disambiguate multiple profiles on the same phone.
- **Conversations classic Service** — used **only** to mint fresh `CH-` SIDs for use as unique `channelId` values on v2 conversations.
- One SMS-capable Twilio number (`AI_NUMBER`) — used by both AI and dispatcher personas.
- `AI_AGENT_PROFILE_ID` (from `bootstrap.py`) — the AI's own Memory profile. It is the **only** profile that carries the `phone` identifier (set to `AI_NUMBER`).
- Local state file `.state/phone_index.json` — created on first `record_profile` call; path overrideable via `PHONE_INDEX_PATH`.

---

## Identifier model

| Where | `chat` | `jobID` (custom, enforceUnique) | `phone` | `mobile` |
| --- | --- | --- | --- | --- |
| Tech-job profile | **not set** (user preference — keep only one unique identifier per profile) | job ID | not set | Trait on `Job` (not an identifier) |
| AI agent profile | — | — | `AI_NUMBER` | trait mirror of `AI_NUMBER` |

Consequences:
- Memory resolves duplicate `POST /Profiles` calls via the `jobID` identifier — reposting the same job ID returns the existing profile.
- Memory **cannot** look up tech profiles by phone. The app's `state.py` fills that gap.
- The `phone` identifier still works for the AI profile only, so `_ai_agent_profile_id` bootstraps by looking it up.

---

## Twilio HTTP surface used

All calls live in `twilio_helpers.py` and hit three base URLs:

| Base | Purpose |
| --- | --- |
| `https://memory.twilio.com/v1` | Trait groups, profiles, identifiers, lookup, recall, observations, summaries |
| `https://conversations.twilio.com/v1` | Classic Conversations — only for minting `channelId` |
| `https://conversations.twilio.com/v2` | Orchestrator conversations, actions, communications |

### Memory (v1)

| Method | Path | Used for |
| --- | --- | --- |
| `GET`  | `/ControlPlane/Stores/{store}/IdentityResolutionSettings` | Read current settings before merging in bootstrap |
| `PUT`  | `/ControlPlane/Stores/{store}/IdentityResolutionSettings` | Overwrite settings (bootstrap adds the `jobID` identifier config + matching rules) |
| `POST` | `/ControlPlane/Stores/{store}/TraitGroups` | Register the `Job` trait group (bootstrap) |
| `GET`  | `/ControlPlane/Stores/{store}/TraitGroups/{name}?includeTraits=true` | Idempotency check for `ensure_trait_group` |
| `PATCH`| `/ControlPlane/Stores/{store}/TraitGroups/{name}` | Merge missing traits into an existing group |
| `POST` | `/Stores/{store}/Profiles` | **Create-or-resolve profile via `jobID`.** Body: `traits.Contact:{jobID, firstName?}` + `traits.Job:{status, startedAt, lastActiveAt, mobile}`. `jobID` identifier has `enforceUnique: true`, so reposting the same jobID returns the existing profile. |
| `GET`  | `/Stores/{store}/Profiles/{id}` | Read traits (esp. `Job.status`, `Job.conversationId`, `Job.channelId`, `Job.mobile`) |
| `PATCH`| `/Stores/{store}/Profiles/{id}` | Merge `Job` state (`status`, `conversationId`, `channelId`, timestamps) |
| `DELETE`| `/Stores/{store}/Profiles/{id}` | Hard delete — Memory does not soft-delete |
| `POST` | `/Stores/{store}/Profiles/Lookup` | `{idType, value}` → list of profile ids. Used for `phone → AI profile` bootstrap only; **not** for routing tech SMS. |
| `POST` | `/Stores/{store}/Profiles/{id}/Recall` | Fetch observations + summaries. Optional `conversationId` scoping; app uses **unscoped** recall so LLM sees full job history across every conversation the profile ever had. |
| `POST` | `/Stores/{store}/Profiles/{id}/Observations` | CINTEL `CLASSIFICATION` results land here (e.g. sentiment labels) |
| `POST` | `/Stores/{store}/Profiles/{id}/ConversationSummaries` | CINTEL `TEXT` results land here (e.g. per-conversation summary) |

### Conversations classic (v1) — channelId mint only

| Method | Path | Used for |
| --- | --- | --- |
| `POST` | `/v1/Services/{svc}/Conversations` | Create a classic Conversation solely to harvest its `CH-` SID → reused as `channelId` on the v2 conversation |

### Conversations v2 (Orchestrator)

| Method | Path | Used for |
| --- | --- | --- |
| `POST` | `/v2/Conversations` | Create a two-participant conversation: `CUSTOMER` (tech, carries `jobID` `profileId`) and `AI_AGENT` (carries AI profile id). Both share the same `channelId`. |
| `GET`  | `/v2/Conversations/{id}` | Read status + participants |
| `PUT`  | `/v2/Conversations/{id}` `{status}` | Set state to `ACTIVE` / `INACTIVE` / `CLOSED`. Closing a conversation triggers CINTEL/Memory extraction per config. |
| `GET`  | `/v2/Conversations/{id}/Communications?PageSize=100` | Read transcript |
| `POST` | `/v2/Conversations/{id}/Actions` | `type: SEND_MESSAGE` — actually dispatches an outbound SMS via Twilio. Response includes `related.messageSid` (`SMxxxx`). |
| `POST` | `/v2/Conversations/{id}/Communications` | Bookkeep a message record. Used for outbound (Actions doesn't auto-record) and for inbound tech SMS. **Outbound records set `resourceId` to the Message SID** so the Communication cross-links to the underlying SMS resource (Console + status callbacks show a unified thread). |

---

## Local state — `state.py`

A tiny JSON-file index keyed by phone number:

```json
{ "+44 7…": ["PR_a1b2", "PR_c3d4", "PR_e5f6"] }
```

- Written on `assign_job` (and `reactivate_job` as a belt-and-suspenders) via `state.record_profile(phone, profile_id)`.
- Read on `assign_job` (to enumerate candidate profiles for status checks), `reactivate_job` (same), `delete_profile` (to forget the id), `/api/state`, and every inbound SMS.
- Cleared on `delete_profile` via `state.forget_profile(phone, profile_id)`.
- Uses `threading.Lock` + atomic tmp-file writes; path overridable via `PHONE_INDEX_PATH` (default `.state/phone_index.json` next to the module).

This is the **only** external state. Everything semantic — status, conversation wiring, observations, summaries — lives on the Memory profile.

---

## Participant model — a design constraint worth naming

- Only **two** participants per conversation: `CUSTOMER` (tech) and `AI_AGENT` (AI).
- Twilio enforces address uniqueness across participants regardless of `channelId`, so a distinct "admin" participant at the AI number is **not allowed**.
- Dispatcher (human) messages are **relayed through the AI participant** with a `[<ADMIN_PERSONA>]` prefix in the body. The prefix is the only provenance marker; `_classify_author` in `main.py` checks the body prefix before the participant type.
- The tech's phone sees one SMS thread from the AI number; SMS groups by peer number on the handset, so mixing AI + dispatcher on one number is a physical constraint the design leans into.

---

## Multi-active jobs — allowed by design

A technician can hold **multiple active jobs concurrently**. Each concurrent active job stands on its own:

- **Its own Memory profile** — one per `jobID`. Observations, summaries, and `Recall` context stay cleanly scoped per job.
- **Its own Orchestrator conversation** — created under a distinct `channelId` (minted from Conversations v1). Twilio permits multiple `ACTIVE` conversations on the same tech phone as long as their `channelId`s differ, so nothing at the platform level blocks it.
- **Its own `Job.conversationId`** trait on the profile — the router reads this to pick the right thread once a job has been chosen.
- **Its own entry in `state.py`** — the local `phone → [profileId, ...]` index lists every profile the tech has ever been assigned to; the routing step filters by live `Job.status == "active"`.

The physical constraint the design leans into: SMS on the tech's handset groups by peer number, and the tech only ever talks to one AI number. So even though multiple conversations exist server-side, the tech sees a single thread. That's the reason routing needs a disambiguation step — the tech can't tell one conversation from another visually.

**Disambiguation rule (Option A — prefix parse).** When the tech's phone has more than one active job, the router requires the first whitespace-delimited token of the inbound SMS body to match one of the active `jobID`s. If it does, that token is stripped and the remainder is routed to the matched job's conversation. If it doesn't, the router replies with a TwiML `<Message>` listing the tech's active job IDs and an example prefix — no message is recorded in any Memory profile until the tech picks. Once the tech has one active job again, the prefix requirement disappears; inbound messages route straight through.

Trade-off worth naming: this puts a small UX burden on the tech (typing the job ID) in exchange for keeping the disambiguation **stateless** on the server. No per-phone "last selected job" side-channel, no TTL, no race with a concurrent `assign_job`. Every inbound message is self-describing.

---

## Worked flow — inbound SMS with multiple active jobs

Concrete walkthrough. Tech `+44 7…` has been assigned jobs `4521` and `4522`; both are `active`, each with its own open conversation.

**State going in:**
- `state.py`: `+44 7… → [PR_4521, PR_4522, PR_4520]` (4520 is a completed job the tech previously held)
- Memory:
  - `PR_4521` — `Job.status="active", conversationId=CX_A`
  - `PR_4522` — `Job.status="active", conversationId=CX_B`
  - `PR_4520` — `Job.status="completed", conversationId=CX_prev`

**Turn 1 — ambiguous inbound.** Tech texts `on site`.

1. Webhook `POST /webhooks/sms/ai` → `_route_inbound_sms(from_="+44 7…", body="on site", ...)`.
2. `state.profiles_for_phone("+44 7…")` → `["PR_4521", "PR_4522", "PR_4520"]`.
3. `find_active_profiles_among(...)` → `[PR_4521 profile, PR_4522 profile]` (PR_4520 is skipped — `Job.status == "completed"`).
4. `len(actives) == 2` → prefix parse:
   - `body.strip().partition(" ")` → `("on", " ", "site")`
   - Token `"on"` (uppercased, stripped of `:,.;`) → **no match** in `{"4521", "4522"}`.
5. Router returns TwiML:
   ```xml
   <Response><Message>You have 2 active jobs: 4521, 4522. Start your message with the Job ID (e.g. "4521 on site").</Message></Response>
   ```
6. **Nothing** is recorded in either job's Memory profile. Neither `_handle_inbound` nor `record_communication` runs. The tech's original "on site" message never enters a conversation transcript.

**Turn 2 — disambiguated inbound.** Tech texts `4522 on site`.

1. Same entry point, `_route_inbound_sms(from_="+44 7…", body="4522 on site", ...)`.
2. Steps 2–3 identical — two active profiles.
3. `len(actives) == 2` → prefix parse:
   - `body.strip().partition(" ")` → `("4522", " ", "on site")`
   - Token `"4522"` → **matches** `PR_4522`.
   - `remainder = "on site"` (jobID stripped).
4. Router picks `PR_4522`, reads `conv_id = CX_B` off the profile's `Job.conversationId`.
5. Calls `_handle_inbound(CX_B, "on site", simulated=False)`:
   - `POST /Conversations/CX_B/Communications` — records `"on site"` as the tech's inbound (no `resourceId` — no send action).
   - `POST /Profiles/PR_4522/Recall` — unscoped, returns observations + summaries scoped to job 4522 only.
   - `list_communications(CX_B)` — history for the LLM, tagged with dispatcher-prefix handling.
   - `llm.generate_reply(...)` → the model sees only `PR_4522`'s context; job 4521's history never leaks.
   - `send_message_action(CX_B, ...)` → real SMS to the tech; response carries `related.messageSid = "SM…"`.
   - `POST /Conversations/CX_B/Communications` with `resourceId="SM…"` — records the AI reply, cross-linked to the underlying SMS.
6. Router returns `<Response/>` (empty TwiML).

**Turn 3 — same conversation, no prefix needed?** Tech texts `heading to next stop`.

- Prefix parse again fails — `"heading"` doesn't match any active jobID — so the router asks again. **The prefix is required on every message while >1 job is active**, because the app carries no "last selected job" state. To end the disambiguation, either complete one of the active jobs (dropping the count to 1) or prefix every message.

**Turn 4 — one job completed.** Dispatcher marks 4521 as completed (`POST /api/complete-job` on `PR_4521`).

- `PR_4521.Job.status → "completed"`, `CX_A → CLOSED`.
- `state.py` still has `PR_4521` in the list — that's fine; the router filters live on `Job.status`.
- Next inbound: `find_active_profiles_among(...)` returns `[PR_4522]` only. `len(actives) == 1` → route the body verbatim (no prefix required, no token stripping). The tech's UX quietly reverts to the single-active behavior.

**Key properties this flow guarantees:**
- **No cross-job context leakage.** `Recall`, `list_communications`, and the LLM's history are all scoped to the chosen profile's conversation. Job 4522's LLM never sees observations from job 4521.
- **No server-side session state.** The disambiguation decision is derived from the current inbound body + current Memory state on every request. Restart the process, wipe an in-memory cache, doesn't matter.
- **No silent misroute.** If the tech forgets the prefix, they get a TwiML reply back — the message is refused, not sent to some default job.
- **The completed job vanishes automatically.** Nothing in the app has to remember "the tech used to prefix; they don't need to anymore" — the branch on `len(actives)` handles it.

---

## Application flows — what each route does

### `POST /api/assign-job`  (the flow the sequence diagram covers)

1. **Create/resolve the profile via `jobID`.** `POST /Profiles` with `Contact:{jobID, firstName}` + `Job:{status:"active", startedAt, lastActiveAt, mobile: TECH_PHONE}`. Because `jobID` is `enforceUnique: true`, reposting the same job ID returns the existing profile (the "resume" case). The response body is the resolved profile with traits — including any prior `Job.conversationId` / `Job.channelId`.
2. **Record the phone→profile mapping locally.** `state.record_profile(TECH_PHONE, profile_id)` — idempotent JSON write.
3. **Reuse an open conversation if possible.** Read `existing_conv_id` and `channel_id` off the POST response (no separate GET). If `existing_conv_id` points at an `ACTIVE|INACTIVE` conversation, use it.
4. **Otherwise create a fresh conversation.** `mint_channel_id` (v1 `POST`) → `create_orchestrator_conversation` (v2 `POST`) with both participants.
5. **One PATCH writes every trait mutation:** `{status:"active", lastActiveAt, conversationId, channelId}` in a single `PATCH /Profiles/{id}` call — no intermediate status-flip.
6. Send welcome via `/Actions` (real SMS from AI number to tech, prefixed with `[<ADMIN_PERSONA>]`) → capture `action.related.messageSid` → `POST /Communications` with `resourceId=messageSid` to bookkeep it.
7. Respond `{profileId, conversationId, resumed}`.

Result: on a first-time assignment the whole path is `POST /Profiles + state write + v1 mint + v2 create + one PATCH + Actions + Communications`. On resume with a still-open conversation it collapses further — no v1 mint, no v2 create.

### `POST /api/admin/send`

- Prefix body with `[<ADMIN_PERSONA>]`, send via `/Actions` from AI participant to tech participant, then `POST /Communications` (with `resourceId=action.related.messageSid`) to record it.

### `POST /api/tech/simulate` and `POST /webhooks/sms/ai`

Router (`_route_inbound_sms`):
1. `state.profiles_for_phone(from_)` — enumerate every profile id the app has ever recorded for this phone.
2. `tw.find_active_profiles_among(STORE_ID, known_ids)` — GET each and keep those with `Job.status == "active"`.
3. Branch on count:
   - **0** → reply `<Message>You're not currently assigned to a job. Please contact dispatch.</Message>`
   - **1** → route the body verbatim into that profile's conversation.
   - **>1** → **multi-active disambiguation**. Parse the first whitespace-delimited token from the body (trailing `:,.;` stripped, case-insensitive). If it matches one of the active `jobID`s, route to that job's conversation with the token stripped from the body. Otherwise reply `<Message>You have N active jobs: A, B, C. Start your message with the Job ID (e.g. "A on site").</Message>`

Simulated and real inbound tech SMS share `_handle_inbound`:
1. `POST /Communications` to record the inbound (no `resourceId` — it's inbound, no send action).
2. `POST /Recall` (unscoped) to hydrate LLM context.
3. Build history from `list_communications`, tagging `[Message from human dispatcher…]` when the body carries the admin prefix.
4. `llm.generate_reply` → send via `/Actions` → `POST /Communications` (with `resourceId=action.related.messageSid`) to record the reply.

Real webhook returns empty TwiML on the happy path; TwiML `<Response><Message>...</Message></Response>` for the 0-active and multi-active-ambiguous cases (all messages are XML-escaped via `xml.sax.saxutils.escape`).

### `POST /api/complete-job`

- `PATCH` profile: `Job.status="completed", completedAt`. Close the v2 conversation. CINTEL fires on close.

### `POST /api/reactivate-job`

- **No longer pauses other actives** — multi-active is allowed, so reactivating one profile does not touch any other.
- `GET` the target profile once to read its current `Job.conversationId` / `channelId`. If the referenced conversation is CLOSED (or gone), mint a new `channelId` and create a fresh v2 conversation on the **same profile**.
- **One combined `PATCH`** writes the target's new state: `{status:"active", lastActiveAt, completedAt:null, conversationId, channelId}`.
- `state.record_profile(TECH_PHONE, body.profileId)` — idempotent belt-and-suspenders in case the state file was wiped.
- Prior summaries are already attached to the profile, so the LLM sees the full history via unscoped `Recall`.

### `DELETE /api/profile/{id}`

- Close the profile's conversation, hard-delete the profile from Memory, and `state.forget_profile(mobile, profile_id)` so the local index stops surfacing it.

### `POST /webhook/cintel`

- Iterates `operatorResults`. `profileId` is pulled from the `CUSTOMER` participant in `executionDetails.participants` — the profileId travels with the participant, so no attribution logic is needed.
- `outputFormat=TEXT` → `POST /ConversationSummaries` on that profile.
- `outputFormat=CLASSIFICATION` → `POST /Observations` on that profile.

### `POST /webhook` (catch-all)

- Form-encoded with `From`/`To`/`Body` → routes through `_route_inbound_sms` (same handler as `/webhooks/sms/ai`).
- JSON body → logged only (Orchestrator status callbacks etc.).

---
