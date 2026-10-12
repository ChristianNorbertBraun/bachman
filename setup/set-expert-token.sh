#!/bin/bash
# Run as an admin user:   sudo -u bachman bash ~bachman/current/setup/set-expert-token.sh
# Stores the bearer token with which Bachman asks the expert (dir 700, file 600). Nothing is printed.
# The token is created on the expert's side, for a client of its own (for Gregory: setup/new-client-token.sh).
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
cd ~
umask 077
D=${XDG_CONFIG_HOME:-$HOME/.config}/bachman/expert
mkdir -p "$D"
read -rsp "Token for the expert: " TOKEN; echo
[ "${#TOKEN}" -ge 32 ] || { echo "ABORT: too short to be a token, nothing was stored"; exit 1; }
case "$TOKEN" in *[!A-Za-z0-9._-]*) echo "ABORT: the token contains unexpected characters, nothing was stored"; exit 1;; esac
printf '%s' "$TOKEN" > "$D/token"
chmod 700 "$D"; chmod 600 "$D/token"
echo "Stored in $D (length: ${#TOKEN} characters)."
