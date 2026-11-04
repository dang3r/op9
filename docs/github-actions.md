# Running tests on GitHub

Every pull request and push to `main` runs the offline pytest suite on Python 3.13 with the dependencies pinned in `uv.lock`. It runs without Twilio or Google credentials.

Live calls run automatically for PRs that dang3r opens from this repository into main, when dang3r opens, updates, reopens, or marks the PR ready for review. The job checks the original actor and the person requesting a rerun before it can access the phone-e2e environment. Fork PRs and other contributors receive offline tests only. Dang3r can also dispatch the live test from main. The live workflow exercises the deployed service using trusted main code. The deployed service must be running passcode mode with the signed Gather callback. A successful run prints the caller CallSid and confirms receipt of digit `9`.

## One-time setup for live calls

Create a GitHub environment named `phone-e2e` and restrict deployments to `main`. Configure the following environment settings.

| Kind | Names | Purpose |
| --- | --- | --- |
| Secrets | `TWILIO_ACCOUNT_SID`, `TWILIO_API_KEY`, `TWILIO_API_SECRET` | Authenticate the test caller to Twilio |
| Secrets | `E2E_CALLER_NUMBER`, `E2E_SERVICE_NUMBER`, `E2E_PASSCODE` | Dial from the dedicated caller to the deployed service and enter the passcode |
| Variable | `E2E_CALLBACK_URL` | Full HTTPS URL ending in `/phone-test/dtmf` on the deployed op9 service |
| Variables | `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_LOG_READER_SERVICE_ACCOUNT` | Full provider resource name and email of the Cloud Logging reader service account |

Configure [Google Workload Identity Federation](https://github.com/google-github-actions/auth#setup) for this repository, restricting the trust relationship to the `main` ref and `phone-e2e` environment. Grant the federated identity permission to impersonate the service account and grant that account Logs View Accessor on the dedicated `receipts` log view. The view reads the one-day `op9-e2e-receipts` bucket, fed by a sink filtered to op9 digit receipts. The runner reads the callback receipt from Cloud Logging and uses short-lived Google credentials.
