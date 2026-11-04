#!/usr/bin/env bash
set -euo pipefail

CFG_FILE="/mnt/hermes-workdir/DOKUMENTUMOK/03_JSON_ADATOK/ai_beallitas.json"
ENV_FILE="/home/menti/HAZ/PAPIRDIGITALOZALAS/.env"
UNRAID_ENV="root@192.168.0.32:/mnt/disks/QM00001/_HOMELAB/_DOCKER/_DOCKER_APPDATA/_PAPERLESS/.env"

usage() {
    echo "Használat: $0 [openai | openai-oauth | ollama | gemini | status]"
    echo ""
    echo "Opciók:"
    echo "  openai        -> Hivatalos OpenAI API kulcs (Home Assistantból átvett, 100% ToS-tiszta, gpt-6-luna)"
    echo "  openai-oauth  -> ChatGPT / Codex OAuth előfizetés (auth.json, gpt-6-luna)"
    echo "  ollama        -> Helyi ingyenes AI Core (192.168.0.77:11434, gemma3:4b)"
    echo "  gemini        -> Google Gemini API"
    echo "  status        -> Jelenlegi aktív AI motor lekérdezése"
    exit 1
}

MODE="${1:-status}"

if [[ "$MODE" == "status" ]]; then
    if [[ -f "$CFG_FILE" ]]; then
        cat "$CFG_FILE"
    else
        grep "^AI_PROVIDER=" "$ENV_FILE"
    fi
    exit 0
fi

if [[ "$MODE" != "openai" && "$MODE" != "openai-oauth" && "$MODE" != "ollama" && "$MODE" != "gemini" ]]; then
    usage
fi

python3 - "$CFG_FILE" "$MODE" <<'PY'
import json, pathlib, sys
cfg_path = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
data = {}
if cfg_path.exists():
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
data["active_provider"] = mode
data.setdefault("openai_model", "gpt-6-luna")
data.setdefault("codex_oauth_model", "gpt-6-luna")
data.setdefault("ollama_model", "gemma3:4b")
data.setdefault("gemini_model", "gemini-2.5-flash")
data.setdefault("auto_fallback", True)
cfg_path.parent.mkdir(parents=True, exist_ok=True)
cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Aktív AI motor sikeresen átállítva erre: {mode} (fájl: {cfg_path})")
PY

if [[ -f "$ENV_FILE" ]]; then
    sed -i "s/^AI_PROVIDER=.*/AI_PROVIDER=${MODE}/" "$ENV_FILE"
    scp -q "$ENV_FILE" "$UNRAID_ENV" 2>/dev/null || true
fi
