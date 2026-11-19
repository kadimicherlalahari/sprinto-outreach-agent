#!/usr/bin/env bash
# Adds the dashboard/cron/Gmail secrets from data/vercel_missing.env to the Vercel project.
# Run it yourself: it logs you in to Vercel (first time) and sends the values to Vercel. Values are never printed.
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="data/vercel_missing.env"
[ -f "$FILE" ] || { echo "Missing $FILE"; exit 1; }
VC="npx --yes vercel@latest"
$VC whoami >/dev/null 2>&1 || $VC login
$VC link --yes --project sprinto-outreach-agent >/dev/null
while IFS='=' read -r key value; do
  [ -z "$key" ] && continue
  for env in production preview; do
    $VC env rm "$key" "$env" --yes >/dev/null 2>&1 || true
    printf '%s' "$value" | $VC env add "$key" "$env" >/dev/null
  done
  echo "added $key (production, preview)"
done < "$FILE"
echo "Done. Tell Claude to redeploy."
