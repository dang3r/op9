# Running tests on GitHub

Pushes to repository branches and fork pull requests run offline tests. After offline tests pass on a feature-branch push to dang3r/op9, staging runs if GitHub identifies dang3r as the pusher and as the actor for any rerun. This applies to signed and unsigned commits. A multi-commit push stages its tip. No PR is required.

The authorized build checks out the exact push-tip SHA on an uncredentialed runner, runs the offline tests, and builds a Linux image from a git archive of that SHA.

A separate runner checks out trusted main, pushes the packaged image to the staging Artifact Registry repository, deploys the image by digest to `op9-staging`, and verifies the revision has 100% of traffic. A single concurrency group covers the deploy and call. GitHub can supersede pending jobs when multiple changes are queued. The running test keeps its slot until completion.

The production number places an outbound call to the staging number. The staging number routes incoming calls to staging's `/voice` endpoint. Staging uses passcode mode, a separate Twilio subaccount and passcode, with SMS disabled. The caller's signed Gather callback goes to production and pytest checks the receipt for that exact caller CallSid. The workflow reports `staging/phone-e2e` on the tested head SHA for use as a required status check. Merging and production deployment are separate operations.

Local phone tests use the numbers and passcode in the local environment.

## One-time setup for live calls

Create a GitHub environment named `phone-e2e` and restrict deployments to `main`. Configure the following environment settings.

| Kind | Names | Purpose |
| --- | --- | --- |
| Secrets | `TWILIO_ACCOUNT_SID`, `TWILIO_API_KEY`, `TWILIO_API_SECRET` | Authenticate the test caller to Twilio |
| Secrets | `E2E_CALLER_NUMBER`, `E2E_SERVICE_NUMBER`, `E2E_PASSCODE` | Production caller number, staging destination number, and staging passcode |
| Variable | `E2E_CALLBACK_URL` | Full HTTPS URL ending in `/phone-test/dtmf` on the trusted production op9 service |
| Variables | `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_LOG_READER_SERVICE_ACCOUNT` | Full provider resource name and email of the Cloud Logging reader service account |

Configure [Google Workload Identity Federation](https://github.com/google-github-actions/auth#setup) for this repository, restricting the trust relationship to the `main` ref and `phone-e2e` environment. Grant the federated identity permission to impersonate the service account and grant that account Logs View Accessor on the dedicated `receipts` log view. The view reads the one-day `op9-e2e-receipts` bucket, fed by a sink filtered to op9 digit receipts. The runner reads the callback receipt from Cloud Logging and uses short-lived Google credentials.

The CI identity also has Artifact Registry Writer on the staging repository, Cloud Run Developer on the staging service, and Service Account User on its staging runtime identity. The runtime can read the two staging secrets. The production Twilio token is kept out of staging.

The staging caller records its incoming audio. After the live test finishes, the runner downloads the completed recording for that caller CallSid and runs Whisper locally. Open the status check on the tested commit to see its Actions run summary, including the transcript and received digit. A failed call still gets a summary. If the recording or transcription is unavailable, the summary says so. Only the signed Gather receipt determines whether the phone assertion passed.

Audio stays in temporary runner storage and Twilio's recording store. The workflow publishes only the text. The restricted test key requires call recording list and recording read permissions.
