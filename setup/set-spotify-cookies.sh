#!/bin/bash
# Run as an admin user:   sudo -u bachman bash set-spotify-cookies.sh   (from a place that user can read)
# Stores the Spotify for Creators session cookies of the podcast login for the service user (dir 700, files 600).
# The values are typed or pasted by the user and are never printed. Re-run it whenever the cookies expire.
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
cd ~
umask 077
D=${XDG_CONFIG_HOME:-$HOME/.config}/bachman/spotify
mkdir -p "$D"

echo "Browser: log in at https://creators.spotify.com, then DevTools > Application > Cookies > https://creators.spotify.com"
echo "The cookie values are not shown while you paste them."
read -rsp "sp_dc:  " SP_DC;  echo
read -rsp "sp_key: " SP_KEY; echo
echo "Show id from the URL: https://creators.spotify.com/pod/show/<SHOW-ID>/episodes"
read -rp  "Show id: " SHOW_ID

for v in SP_DC SP_KEY SHOW_ID; do
  val=${!v}
  [ -n "$val" ] || { echo "ABORT: $v is empty, nothing was stored"; exit 1; }
  case "$val" in *[[:space:]]*) echo "ABORT: $v contains whitespace, nothing was stored"; exit 1;; esac
done

printf '%s' "$SP_DC"   > "$D/sp_dc"
printf '%s' "$SP_KEY"  > "$D/sp_key"
printf '%s' "$SHOW_ID" > "$D/show_id"
chmod 700 "$D"; chmod 600 "$D"/*
echo "Stored in $D (lengths: sp_dc ${#SP_DC}, sp_key ${#SP_KEY}, show id ${#SHOW_ID} characters)."
ls -la "$D"
