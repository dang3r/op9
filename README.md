# op9

Building entry operator — accepts calls via Twilio and opens the door by sending DTMF tones. Dubbed `op9` because `9` is the tone that unlocks the door.

## How it works

```text
Intercom → Twilio number → POST /voice → TwiML → DTMF "9" → door unlocks
```

## Deploy to Cloud Run

Non-secret config lives in `env.yaml`. Secrets live in Secret Manager, backed by gitignored files in `secrets/`.

```bash
# Push secrets to Secret Manager
./push-secrets.sh                 # all three
./push-secrets.sh agent-context   # or just the one you changed

# `--timeout=900` is needed for `voice-agent` mode: Cloud Run's request timeout applies to WebSockets and defaults to 5 minutes.
gcloud run deploy op9 \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --timeout=900 \
  --max-instances=1 \
  --env-vars-file env.yaml \
  --set-secrets "ANTHROPIC_API_KEY=anthropic-api-key:latest,TWILIO_AUTH_TOKEN=twilio-auth-token:latest,AGENT_CONTEXT=agent-context:latest,AGENT_PEOPLE=agent-people:latest,TWILIO_ACCOUNT_SID=twilio-account-sid:latest,NOTIFY_SMS_TO=notify-sms-to:latest"

# Gather logs
gcloud run services logs read op9 --project operator9 --region us-central1 --limit 50
```

# Notes

## Mode 1 - `auto`

Automatically open the door when prompted. This is the most insecure but was the first tested mode

## Mode 2 - `passcode`

Users have to enter a configurable passcode, it is submitted via DTFM and approved/disapproved.

## Mode 3 - `voice-agent`

An LLM talks to the visitor and decides whether to let them in.

```text
Intercom → Twilio → /voice → <Connect><ConversationRelay wss://…/relay>
                                          ↕
                              /relay WebSocket ← LLM loop → sendDigits "ww9"
```

Twilio's **ConversationRelay** does the speech-to-text and text-to-speech and bridges the call to our `/relay` WebSocket. We only ever exchange JSON text frames — no audio handling on our side. That is also why swapping in ElevenLabs later is one TwiML attribute (`AGENT_TTS_PROVIDER=ElevenLabs` + `AGENT_VOICE=<id>`) and no code change.

### The model gets exactly two tools

| Tool | Effect |
|---|---|
| `open_door(reason)` | send `{"type":"sendDigits","digits":"ww9"}`, hang up |
| `deny_entry(reason)` | say goodbye, hang up |

Asking the visitor a question is deliberately **not** a tool — plain assistant text is already spoken to the caller, so a question needs no tool at all. That leaves the tool surface as exactly the two decisions with a security consequence, each carrying a `reason` that lands in the logs.

### Fail closed

The door is opened by **one line** in `app.py`, reached only when the model calls `open_door`. Every other way out of the relay loop — an Anthropic outage, a malformed frame, the turn cap, the caller hanging up, a bug — falls through to a `finally` with the door shut. The correct behavior is the one you get by doing nothing.

### Never send `end` after `sendDigits`

Twilio plays `sendDigits` asynchronously. An `{"type":"end"}` frame immediately behind it tears the call down mid-tone, so the intercom hears a truncated sequence and the door does not trip — while the model has already approved and the SMS has already gone out. `OPEN_DIGITS` is `ww9`, and each `w` is a 0.5s pause, so the tones need a full second before the `9` even starts.

This was a live bug: `sendDigits` at 03:15:38, connection closed at 03:15:40, approval text received, door shut. It is also why the closing line was never audible — `end` was racing the audio all along, and the earlier attempt at a sleep *inside* `_hang_up` could not fix it, because that delays `end` without letting the already-queued audio finish.

`_open_and_release` sends the tones, waits `OPEN_DIGITS_SETTLE` seconds, and returns **without** an `end`. The call winds down when the caller hangs up or Twilio drops it. `auto` mode never had this problem because `<Play digits>` + `<Hangup>` is sequenced by Twilio itself; only the WebSocket modes need the explicit settle.

## Mode 4 - `voice-agent-people`

Per-person challenge with a per-person code fallback, and a live SMS escalate for unknowns.

```text
greeting → visitor says a name
  → classify against the roster                       (model, names only)
      ├─ known    → ONE random question from THEIR corpus
      │              (picked in code; model only sees that Q+A)
      │              ├─ pass → open (sendDigits)
      │              └─ fail → "Please enter your code."
      │                        DTMF digits → compare   (app.py, never the model)
      │                          ├─ match → open
      │                          └─ no    → deny
      └─ unknown  → model may ask_resident or deny_entry
                     ask_resident → SMS you "Reply YES or NO"
                                     ├─ YES → open
                                     ├─ NO  → deny
                                     └─ timeout → deny
```

It runs on its own `/relay-people` socket rather than as a branch inside `/relay`.

### Why the roster is JSON, and where each secret lives

`AGENT_PEOPLE` is JSON (in Secret Manager as `agent-people`) rather than the freeform prose of `AGENT_CONTEXT`, because unlike the facts it is read by *code*: the loop looks a person up, picks one of their questions at random, and compares their code. It holds door codes (e.g. birth year), so treat it as a credential store — `secrets/` is gitignored, and `./push-secrets.sh agent-people` ships a new version.

```json
[{"name": "Alex", "relation": "brother", "aliases": ["Al"], "code": "1990",
  "questions": [{"ask": "What was the name of the cat we grew up with?", "answer": "Miso"}]}]
```

First name alone is enough for the classifier; adding last names to the roster is fine (`"Daniel"` still matches `"Daniel Cardoza"`).

### Two properties hold this up

**The model never sees a code.** `build_person_prompt` passes one question and omits the code entirely, so the comparison is a `hmac.compare_digest` in `app.py` and no amount of talking can extract a secret that was never in the context.

**Isolation is structural, not instructed.** Only the matched person's randomly chosen question enters the prompt. The model cannot leak what it was never given.

The name-matching is done by the model rather than by string distance because the input is a phone-quality transcript: "it's Hannah" arrives as "its Hana" or "Han uh". Only names are sent to that call, and the label it returns is looked up in code, so a bad match costs at worst the wrong person's question — which still has to be answered.

### Unknown names: `ask_resident`, not a keypad

An unrecognized visitor is never offered a code — otherwise the attack is "say a name nobody recognizes, then brute-force four digits." Instead the model gets a tool, `ask_resident`, that texts `NOTIFY_SMS_TO` with what they claimed and waits for your reply (`YES` / `NO`, also `Y`/`N`/`OPEN`/`DENY`). Timeout (`AGENT_APPROVAL_TIMEOUT`, default 60s) denies. Only texts from your notify number count. A second unknown while one is already waiting is denied immediately so YES/NO stays unambiguous.

Point the Twilio number's **Messaging** webhook at `POST https://<service>/sms`. Approval state is process-local, so keep Cloud Run at `--max-instances=1` for this path or a reply can land on a different instance than the open call and time out (fail closed).

The code prompt for known people is likewise a fixed line that says nothing about the answer. Telling someone they were wrong is a free oracle; the prompt in `agent.py` refuses to grade out loud and the fallback must not grade for it.

### Two Twilio defaults have to be overridden

| Attribute | Default | Why it must change |
|---|---|---|
| `dtmfDetection` | off | Without it Twilio never sends keypresses to the socket at all |
| `reportInputDuringAgentSpeech` | `"none"` since May 2025 | Digits pressed while "enter your code" is still playing are **discarded** — and people start typing immediately, so a correct code would read as a wrong one |

ConversationRelay sends one frame per keypress (`{"type":"dtmf","digit":"4"}`) and never signals completion — there is no `<Gather numDigits>` here. So `_collect_code` owns the buffer, the length check, and a per-digit idle timeout (`AGENT_CODE_TIMEOUT`); `*` clears a fumbled entry. `AGENT_CODE_ATTEMPTS` defaults to 1, because four digits with unlimited retries is a keypad brute-force.

### Fail closed

Same property as Mode 3, with three opening lines instead of one — question passed, code matched, or resident SMS YES. Every other exit (turn cap, timeout, wrong code, SMS timeout, exception, hang-up, unauthenticated socket) falls through to the `finally` with the door shut, and `test_relay.py` asserts exactly that for each.

## Post-call SMS

After every completed call — in any mode, on approve, deny, hang-up, or error — a text summary is sent to a personal number. It carries who called, the verdict (`APPROVED` / `DENIED` / `NO DECISION`), the time in `America/New_York` (EST/EDT), the decision's reason when there is one, and, in voice-agent mode, the full visitor/operator transcript.

The SMS is sent *from* the service's own Twilio number (read off each webhook — the number that receives the buzz is SMS-capable), so there is no separate from-number to configure. Sending is best-effort: it authenticates with `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` and any failure is logged and swallowed, never touching the door decision. It stays off entirely unless `TWILIO_ACCOUNT_SID` and `NOTIFY_SMS_TO` are both set.

Set `E2E_CALLER_NUMBER` on the deployed service to the test caller’s E.164 number. Summaries for that caller start with `[TEST]`, including denied calls.

In passcode mode, the handler waits for Twilio to accept the notification before returning the opening or denial TwiML. This keeps notification submission inside the active Cloud Run request so CPU throttling after the response cannot stall it. It adds the Twilio API round-trip to the door response. The HTTP client uses a three-second timeout with no retries; sending failures still allow the door response. Other modes retain their existing background notification behavior.

To measure latency, compare the `passcode: ... matched=...` log with `notify: accepted ... sid=... status=... elapsed_ms=...`. The elapsed time measures submission to Twilio, not delivery to the handset. Use the logged message SID to inspect its creation, send, and delivery-status update timestamps in Twilio. Compare those with when the notification appears on your phone.

## Live phone end-to-end test

`tests/test_phone_e2e.py` calls the deployed op9 number from a separate Twilio number, waits three seconds for the prompt, sends the correct passcode, and uses `<Gather>` to receive the returned unlock digit. The target must be running **passcode mode**. This verifies the telephone path and received DTMF, rather than just checking the server's opening decision. It does not exercise the physical lock or assert SMS delivery.

Set these in your gitignored `.env` (see `.env.example`):

```dotenv
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
E2E_CALLER_NUMBER=+15550000001
E2E_SERVICE_NUMBER=+15550000002
E2E_PASSCODE=your_current_passcode
E2E_CALLBACK_URL=https://op9-945926743915.us-central1.run.app/phone-test/dtmf
E2E_GCP_PROJECT=operator9
E2E_CLOUD_RUN_SERVICE=op9
```

The caller number must belong to your Twilio account and support voice. The service number is the existing op9 number. You can use `TWILIO_API_KEY` and `TWILIO_API_SECRET` together instead of the auth token for local API authentication; the test does not use your Twilio CLI profile. The deployed service must have `TWILIO_AUTH_TOKEN` to validate callback signatures. Normal Twilio call charges apply, and the service sends its normal notification for the call.

Deploy the `/phone-test/dtmf` route before running the test. It validates Twilio's signature, logs the actual received digit with the caller CallSid, and returns Hangup. It never resolves approvals or changes the door state. The runner uses your local **gcloud login with Cloud Logging read permission** to poll for this exact call's receipt. No tunnel, additional service, or in-memory result store is needed. Log access is checked before placing a call.

```bash
RUN_PHONE_E2E=1 uv run pytest tests/test_phone_e2e.py -v -s --tb=short
```

Without `RUN_PHONE_E2E=1`, it skips without placing a call. Leave the opt-in out of `.env` so an ordinary test run stays offline. Run offline checks with:

```bash
MODE=auto uv run pytest -m 'not phone_e2e' -q
```

Each live run saves `result.json` under the gitignored `artifacts/phone-e2e/<run>/` directory (override with `E2E_ARTIFACT_DIR`). It includes the caller CallSid, received digit, result, and total duration; it excludes credentials and the passcode. The runner waits up to 90 seconds for the receipt, including Cloud Logging ingestion delay. API calls have separate timeouts. Cleanup attempts to stop an active call even on failure. The time until a receipt is observed includes log polling and is not a precise door-opening latency measurement.

The initial audio-recording test received no audible unlock tone even though the service accepted the passcode. The current test collects DTMF signaling directly, and no longer records calls. The offline audio detector and synthetic fixtures remain available for investigation. Old recordings remain in Twilio and local artifact directories until deleted.
