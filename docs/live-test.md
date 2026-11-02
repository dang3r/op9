# Live phone end-to-end test

`tests/test_phone_e2e.py` calls the deployed op9 number from a separate Twilio number, waits three seconds for the prompt, sends the correct passcode, and uses `<Gather>` to receive the returned unlock digit. The target must be running **passcode mode**. This verifies the telephone path and received DTMF, rather than just checking the server's opening decision. It does not exercise the physical lock or assert SMS delivery.

Set these in the repository root’s gitignored `.env` (see [the environment example](../.env.example)):

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
