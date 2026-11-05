# Deploy the passcode service

Run these commands from the repository root with gcloud authenticated for the target project. The checked-in [env.yaml](../env.yaml) selects the experimental `voice-agent-people` mode. The command below selects `passcode` explicitly.

## Configure Cloud Run

Create the following Secret Manager secrets, give the Cloud Run runtime service account access, and add the secret values. [push-secrets.sh](../push-secrets.sh) documents the creation and IAM commands. If you maintain values in the gitignored `secrets/` directory, the script can publish each file as a new version.

| Environment variable | Secret name | Used for |
| --- | --- | --- |
| `PASSCODE` | `passcode` | Check the digits entered by the visitor |
| `TWILIO_AUTH_TOKEN` | `twilio-auth-token` | Verify webhook signatures and authenticate SMS requests |
| `TWILIO_ACCOUNT_SID` | `twilio-account-sid` | Authenticate SMS requests |
| `NOTIFY_SMS_TO` | `notify-sms-to` | Deliver summaries to the resident |

```bash
./push-secrets.sh passcode twilio-auth-token twilio-account-sid notify-sms-to

gcloud run deploy op9 \
  --source . \
  --project operator9 \
  --region us-central1 \
  --allow-unauthenticated \
  --update-env-vars 'MODE=passcode,DEBUG_DTMF=false,OPEN_DIGITS=ww9' \
  --update-secrets 'PASSCODE=passcode:latest,TWILIO_AUTH_TOKEN=twilio-auth-token:latest,TWILIO_ACCOUNT_SID=twilio-account-sid:latest,NOTIFY_SMS_TO=notify-sms-to:latest'
```

`DEBUG_DTMF=false` keeps raw passcodes out of logs. Each `w` in `OPEN_DIGITS=ww9` pauses for half a second before Twilio plays `9`. To label test calls, also set the non-secret `E2E_CALLER_NUMBER` on Cloud Run to your separate test number in E.164 format.

Set the Twilio service number’s incoming voice webhook to `POST https://<service-host>/voice`. op9 returns the `/passcode` URL to Twilio in its response. The [live test](live-test.md) also uses `/phone-test/dtmf`.

## Check a notification

```bash
gcloud run services logs read op9 --project operator9 --region us-central1 --limit 50
```

Compare the `passcode: ... matched=...` line with `notify: accepted ... sid=... status=... elapsed_ms=...`. This measures submission to Twilio. Look up that message SID in Twilio to see when it was sent and when its delivery status changed. The HTTP client uses a three-second timeout with no retries. If sending fails, the call still gets its opening or denial response.
