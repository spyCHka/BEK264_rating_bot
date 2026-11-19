#!/usr/bin/env bash
set -euo pipefail

bundle=$1
api=https://console.altare.gg/api
auth="Authorization: Bearer ${ALTARE_TOKEN:?}"
api_get() { curl --silent --show-error --fail --max-time 45 -H "$auth" "$@"; }
api_post() { api_get -H 'Content-Type: application/json' --data-binary "$2" "$1"; }

# The operator selects exactly one server when creating the token.
identity=$(api_get "$api/api-tokens/self")
team=$(jq -er '.team.id | select(type=="string" and test("^[a-zA-Z0-9_-]+$"))' <<<"$identity")
server=$(jq -er '.serverIds | select(type=="array" and length==1) | .[0] | select(type=="string" and test("^[a-zA-Z0-9_-]+$"))' <<<"$identity") || {
  echo "В Altare выбери Only some apps и укажи один сервер бота." >&2
  exit 1
}
base="$api/tenants/$team/servers/$server"
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
digest=$(sha256sum "$bundle" | cut -d ' ' -f 1)
release="bek264-$digest.pyz"
cat > "$tmpdir/main.py" <<PY
import os
import sys
from pathlib import Path

bundle = Path(__file__).resolve().with_name("$release")
os.execv(sys.executable, [sys.executable, str(bundle)])
PY

# Keep the previous entry point locally until the new one has been verified.
api_get --get --data-urlencode 'path=/main.py' "$base/files/contents" |
  jq -ej '.content | select(type=="string" and length>0)' > "$tmpdir/previous.py"

exists=$(api_get --get --data-urlencode 'path=/' "$base/files" |
  jq -r --arg name "$release" 'any(.items[]; .name == $name and .isFile)')
if [[ "$exists" != true ]]; then
  upload=$(api_post "$base/files/uploads/url" '{"directory":"/"}' | jq -er '.url')
  curl --proto '=https' --silent --show-error --fail --max-time 90 -o /dev/null \
    -F "files=@$bundle;filename=$release" "$upload"
fi
download=$(api_get --get --data-urlencode "path=/$release" "$base/files/download" | jq -er '.url')
curl --proto '=https' --proto-redir '=https' --location --silent --show-error --fail --max-time 90 \
  -o "$tmpdir/received.pyz" "$download"
cmp --silent "$bundle" "$tmpdir/received.pyz" || { echo "Файл на сервере отличается от сборки." >&2; exit 1; }

write_entry() {
  api_post "$base/files/write" "$(jq -n --rawfile code "$1" '{path:"/main.py",content:$code}')" > /dev/null
}
verify_entry() {
  api_get --get --data-urlencode 'path=/main.py' "$base/files/contents" |
    jq -ej '.content' | cmp --silent "$1" -
}
if ! cmp --silent "$tmpdir/main.py" "$tmpdir/previous.py"; then
  if ! write_entry "$tmpdir/main.py" || ! verify_entry "$tmpdir/main.py"; then
    echo "Ошибка записи main.py. Восстанавливаю предыдущую точку входа." >&2
    write_entry "$tmpdir/previous.py" && verify_entry "$tmpdir/previous.py"
    exit 1
  fi
fi
api_post "$base/power" '{"signal":"restart"}' | jq -e '.ok == true' > /dev/null
echo "Altare принял команду перезапуска контейнера бота."
