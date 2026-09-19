#!/usr/bin/env bash
set -euo pipefail

API_URL="https://openrouter.ai/api/v1/chat/completions"
SYSTEM_PROMPT="Reply in plain text only. Do not use Markdown, code fences, bullet symbols, or special formatting unless explicitly asked."

usage() {
  cat <<EOF
Usage: $(basename "$0") -k KEY -m MODEL "your prompt here"

Options:
  -k, --key KEY       OpenRouter API key (required)
  -m, --model MODEL   Model ID, e.g. deepseek/deepseek-chat-v3.1 (required)
  -h, --help          Show this help and exit

Examples:
  $(basename "$0") -k sk-or-... -m deepseek/deepseek-chat-v3.1 "Explain useState"
  $(basename "$0") -k sk-or-... -m deepseek/deepseek-chat-v3.1:free "Hello"
EOF
}

AI_KEY=""
MODEL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -k|--key)
      [[ -n "${2:-}" ]] || { usage >&2; exit 2; }
      AI_KEY="$2"; shift 2 ;;
    -m|--model)
      [[ -n "${2:-}" ]] || { usage >&2; exit 2; }
      MODEL="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift; break ;;
    -*)
      usage >&2; exit 2 ;;
    *)
      break ;;
  esac
done

PROMPT="$*"

# Require all three. Any missing -> print help and exit.
if [[ -z "$AI_KEY" || -z "$MODEL" || -z "$PROMPT" ]]; then
  usage >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required but not installed." >&2
  exit 1
fi

payload=$(jq -n \
  --arg model  "$MODEL" \
  --arg sys    "$SYSTEM_PROMPT" \
  --arg prompt "$PROMPT" \
  '{
    model: $model,
    messages: [
      { role: "system", content: $sys },
      { role: "user",   content: $prompt }
    ],
    max_tokens: 1024,
    stream: false
  }')

response=$(curl -sS "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AI_KEY" \
  -d "$payload")

if ! jq -e . >/dev/null 2>&1 <<<"$response"; then
  echo "Error: API returned a non-JSON response:" >&2
  echo "$response" >&2
  exit 1
fi

if jq -e '.error' >/dev/null 2>&1 <<<"$response"; then
  echo "API error:" >&2
  jq -r '.error.message // .error' <<<"$response" >&2
  exit 1
fi

jq -r '.choices[0].message.content // empty' <<<"$response"
