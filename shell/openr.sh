
exec "$(dirname "$0")/chat.sh" \
  -k $(cat idk.txt)$(cat idk2.txt) \
  -m 'deepseek/deepseek-chat-v3.1' \
  "$@"


