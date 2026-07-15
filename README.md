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
  --env-vars-file env.yaml \
  --set-secrets "ANTHROPIC_API_KEY=anthropic-api-key:latest,TWILIO_AUTH_TOKEN=twilio-auth-token:latest,AGENT_CONTEXT=agent-context:latest,TWILIO_ACCOUNT_SID=twilio-account-sid:latest,NOTIFY_SMS_TO=notify-sms-to:latest"

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

## Post-call SMS

After every completed call — in any mode, on approve, deny, hang-up, or error — a text summary is sent to a personal number. It carries who called, the verdict (`APPROVED` / `DENIED` / `NO DECISION`), the time in `America/New_York` (EST/EDT), the decision's reason when there is one, and, in voice-agent mode, the full visitor/operator transcript.

The SMS is sent *from* the service's own Twilio number (read off each webhook — the number that receives the buzz is SMS-capable), so there is no separate from-number to configure. Sending is best-effort: it authenticates with `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` and any failure is logged and swallowed, never touching the door decision. It stays off entirely unless `TWILIO_ACCOUNT_SID` and `NOTIFY_SMS_TO` are both set.
