# Running tests on GitHub

Every pull request and push to `main` runs the offline pytest suite on Python 3.13 with the dependencies pinned in `uv.lock`. It runs without Twilio or Google credentials.

To place one real test call, open **Actions → Tests → Run workflow**, select `main`, and enable **Place one billable Twilio test call**. This runs after the offline suite passes. The deployed service must be running passcode mode with the signed Gather callback. A successful run prints the caller CallSid and confirms receipt of digit `9`.

## One-time setup for live calls

Create a GitHub environment named `phone-e2e` and restrict deployments to `main`. Configure the following environment settings.

| Kind | Names | Purpose |
| --- | --- | --- |
| Secrets | `TWILIO_ACCOUNT_SID`, `TWILIO_API_KEY`, `TWILIO_API_SECRET` | Authenticate the test caller to Twilio |
| Secrets | `E2E_CALLER_NUMBER`, `E2E_SERVICE_NUMBER`, `E2E_PASSCODE` | Dial from the dedicated caller to the deployed service and enter the passcode |
| Variable | `E2E_CALLBACK_URL` | Full HTTPS URL ending in `/phone-test/dtmf` on the deployed op9 service |
| Variables | `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_LOG_READER_SERVICE_ACCOUNT` | Full provider resource name and email of the Cloud Logging reader service account |

Configure [Google Workload Identity Federation](https://github.com/google-github-actions/auth#setup) for this repository, restricting the trust relationship to the `main` ref and `phone-e2e` environment. Grant the federated identity permission to impersonate the service account and grant that account Logs Viewer in the `operator9` project. The runner reads the callback receipt from Cloud Logging and uses short-lived Google credentials.
