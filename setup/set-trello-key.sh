#!/bin/bash
# Run as an admin user:   sudo -u bachman bash ~bachman/current/setup/set-trello-key.sh
# Stores the Trello API key and token for the service user (dir 700, files 600). Nothing is printed.
#   Key:   https://trello.com/power-ups/admin  ->  New (any name, your workspace)  ->  API key  ->  Generate a new API key
#   Token: on that page, the link "Token" next to the key; or open
#          https://trello.com/1/authorize?expiration=never&scope=read&response_type=token&name=Bachman&key=<YOUR-KEY>
#          (scope=read,write once the tools that change cards exist)
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
cd ~
umask 077
D=${XDG_CONFIG_HOME:-$HOME/.config}/bachman/trello
mkdir -p "$D"
read -rsp "API key: " KEY;   echo
read -rsp "Token:   " TOKEN; echo
for v in KEY TOKEN; do
  val=${!v}
  [ -n "$val" ] || { echo "ABORT: $v is empty, nothing was stored"; exit 1; }
  case "$val" in *[!A-Za-z0-9]*) echo "ABORT: $v must only contain letters and digits, nothing was stored"; exit 1;; esac
done
printf '%s' "$KEY"   > "$D/key"
printf '%s' "$TOKEN" > "$D/token"
chmod 700 "$D"; chmod 600 "$D/key" "$D/token"
echo "Stored in $D (lengths: key ${#KEY}, token ${#TOKEN} characters)."
