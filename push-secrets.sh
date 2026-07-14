#!/usr/bin/env bash
# Push the files in secrets/ to Secret Manager as new versions.
#
#   ./push-secrets.sh            # push all three
#   ./push-secrets.sh agent-context   # push just one
#
# Edit secrets/agent-context and run this whenever a fact goes stale, a fact
# gets said out loud at a party, or you rotate a key. The deploy pins :latest,
# so the next deploy picks the new version up.
#
# Values are piped from files, never passed as arguments — so they never land in
# argv, shell history, or a process listing.

set -euo pipefail

PROJECT="${GCP_PROJECT:-operator9}"
SECRETS=("anthropic-api-key" "twilio-auth-token" "agent-context")

cd "$(dirname "$0")"

if [[ $# -gt 0 ]]; then
  SECRETS=("$@")
fi

for s in "${SECRETS[@]}"; do
  f="secrets/$s"
  if [[ ! -f "$f" ]]; then
    echo "  SKIP $s — no such file: $f" >&2
    continue
  fi
  if [[ ! -s "$f" ]]; then
    echo "  SKIP $s — file is empty (refusing to push an empty secret)" >&2
    continue
  fi
  gcloud secrets versions add "$s" --data-file="$f" --project="$PROJECT" >/dev/null
  echo "  pushed $s ($(wc -c <"$f" | tr -d ' ') bytes)"
done

echo
echo "Deploy to pick these up:"
echo "  gcloud run deploy op9 --source . --region us-central1 \\"
echo "    --allow-unauthenticated --timeout=900 --env-vars-file env.yaml \\"
echo "    --set-secrets \"ANTHROPIC_API_KEY=anthropic-api-key:latest,TWILIO_AUTH_TOKEN=twilio-auth-token:latest,AGENT_CONTEXT=agent-context:latest\""
