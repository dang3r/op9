# op9

Building entry operator — accepts intercom calls via Twilio and opens the door by sending DTMF tones.

## How it works

```text
Intercom → Twilio number → POST /voice → TwiML → DTMF "9" → door unlocks
```

Every call is accepted automatically. Recordings are stored in Twilio (not locally).

## Setup

```bash
uv sync
cp .env.example .env   # add TWILIO_AUTH_TOKEN
uv run uvicorn app:app --reload --port 8000
```

Point your Twilio number's voice webhook to `https://your-host/voice` (POST).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TWILIO_AUTH_TOKEN` | — | Twilio auth token (webhook signature validation) |
| `OPEN_DIGITS` | `ww9` | DTMF digits to play (`w` = half-second pause) |
| `RECORD_CALLS` | `true` | Record calls via Twilio |

## Deploy to Cloud Run

```bash
gcloud run deploy op9 \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "OPEN_DIGITS=ww9,RECORD_CALLS=true,TWILIO_AUTH_TOKEN=..."
```

Recordings appear in the Twilio console under **Monitor → Logs → Call logs**.

## Warning

This MVP opens the door for every caller. Use only for testing or short windows until phrase/code/manual modes are added.
