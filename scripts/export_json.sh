#!/usr/bin/env bash
# Paperless-ngx Post-Consume Script
# Azonnal jelzi az AI + JSON szinkronizáló szolgáltatásnak, hogy új dokumentum érkezett.

set -e

JSON_OUT_DIR="/usr/src/paperless/json_output"
mkdir -p "${JSON_OUT_DIR}/osszes"

if [ -n "${DOCUMENT_ID:-}" ]; then
    echo "${DOCUMENT_ID}" > "${JSON_OUT_DIR}/.trigger_sync_${DOCUMENT_ID}"
    date -Iseconds > "${JSON_OUT_DIR}/.last_consume_event"
fi
