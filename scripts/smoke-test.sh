#!/usr/bin/env bash
set -euo pipefail
: "${AGENTMESH_URL:?Set AGENTMESH_URL}"
: "${AGENTMESH_TOKEN:?Set AGENTMESH_TOKEN}"
access=()
[[ -n "${AGENTMESH_CF_ACCESS_CLIENT_ID:-}" ]] && access=(-H "CF-Access-Client-Id: $AGENTMESH_CF_ACCESS_CLIENT_ID" -H "CF-Access-Client-Secret: $AGENTMESH_CF_ACCESS_CLIENT_SECRET")
curl -fsS "${access[@]}" "$AGENTMESH_URL/health"; echo
curl -fsS "${access[@]}" -H "Authorization: Bearer $AGENTMESH_TOKEN" "$AGENTMESH_URL/v1/whoami"; echo
