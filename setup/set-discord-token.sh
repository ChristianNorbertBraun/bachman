#!/bin/bash
# Run as an admin user:   sudo -u bachman bash ~bachman/current/setup/set-discord-token.sh [instance]
# Stores the Discord bot token for one Discord connector (dir 700, file 600). Nothing is printed.
# `instance` is the name of its table under [connectors] in config.toml; the default is discord.
#   Token: https://discord.com/developers/applications -> your application -> Bot -> Reset Token
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
cd ~
umask 077
I=${1:-discord}
case "$I" in *[!a-z0-9]*|"") echo "ABORT: the instance name is lowercase letters and digits"; exit 1;; esac
D=${XDG_CONFIG_HOME:-$HOME/.config}/bachman/$I
mkdir -p "$D"
read -rsp "Bot token: " TOKEN; echo
[ -n "$TOKEN" ] || { echo "ABORT: the token is empty, nothing was stored"; exit 1; }
case "$TOKEN" in *[!A-Za-z0-9._-]*) echo "ABORT: the token contains unexpected characters, nothing was stored"; exit 1;; esac
printf '%s' "$TOKEN" > "$D/token"
chmod 700 "$D"; chmod 600 "$D/token"
echo "Stored in $D (length: ${#TOKEN} characters)."
