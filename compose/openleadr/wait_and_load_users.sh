#!/bin/bash
# Wait for OpenLEADR migrations (user table) then load lab OAuth clients.
set -euo pipefail

echo "Waiting for OpenLEADR schema (user table)..."
for i in $(seq 1 60); do
  if psql -v ON_ERROR_STOP=1 -c "SELECT 1 FROM \"user\" LIMIT 1;" >/dev/null 2>&1; then
    echo "Schema ready."
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "Timed out waiting for VTN migrations." >&2
    exit 1
  fi
  sleep 2
done

if psql -tAc "SELECT 1 FROM user_credentials WHERE client_id='bl-client'" | grep -q 1; then
  echo "Lab users already loaded."
  exit 0
fi

echo "Loading OpenLEADR lab users (bl-client / ven-client)..."
psql -v ON_ERROR_STOP=1 -f /users.sql
echo "Done."
