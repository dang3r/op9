# op9

Building entry operator — accepts calls via Twilio and opens the door by sending DTMF tones. Dubbed `op9` because `9` is the tone that unlocks the door.

## How it works

```text
Intercom → Twilio number → POST /voice → TwiML → DTMF "9" → door unlocks
```

## Deploy to Cloud Run

For config see `config.py`

```bash
gcloud run deploy op9 \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "OPEN_DIGITS=ww9,RECORD_CALLS=true,TWILIO_AUTH_TOKEN=..."
```

```bash
gcloud run services logs read op9 --project operator9 --region us-central1 --limit 50
```

Recordings appear in the Twilio console under **Monitor → Logs → Call logs**.

# Notes

## Mode 1 - `auto`

Automatically open the door when prompted. This is the most insecure but was the first tested mode

## Mode 2 - `passcode`

Users have to enter a configurable passcode, it is submitted via DTFM and approved/disapproved.
