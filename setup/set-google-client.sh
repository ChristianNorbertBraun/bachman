#!/bin/bash
# Run as an admin user:   sudo -u bachman bash ~bachman/current/setup/set-google-client.sh
# Stores the OAuth client (type "Desktop app") from your Google Cloud project for the service user
# (dir 700, files 600). The values are typed or pasted by the user and are never printed.
# Afterwards sign in once:  sudo -u bachman env PYTHONPATH=/home/bachman/current python3 -m bachman google-login
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
cd ~
umask 077
D=${XDG_CONFIG_HOME:-$HOME/.config}/bachman/google
mkdir -p "$D"

echo "Google Cloud Console > Google Auth Platform > Clients > your Desktop client"
read -rp  "Client ID:     " CLIENT_ID
read -rsp "Client secret: " CLIENT_SECRET; echo

for v in CLIENT_ID CLIENT_SECRET; do
  val=${!v}
  [ -n "$val" ] || { echo "ABORT: $v is empty, nothing was stored"; exit 1; }
  case "$val" in *[[:space:]]*) echo "ABORT: $v contains whitespace, nothing was stored"; exit 1;; esac
done
case "$CLIENT_ID" in *.apps.googleusercontent.com) ;; *) echo "ABORT: the client ID should end in .apps.googleusercontent.com"; exit 1;; esac

printf '%s' "$CLIENT_ID"     > "$D/client_id"
printf '%s' "$CLIENT_SECRET" > "$D/client_secret"
chmod 700 "$D"; chmod 600 "$D"/client_id "$D"/client_secret
echo "Stored in $D (lengths: client ID ${#CLIENT_ID}, secret ${#CLIENT_SECRET} characters)."
