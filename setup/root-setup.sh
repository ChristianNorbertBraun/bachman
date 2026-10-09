#!/bin/bash
# Run as an admin user: sudo bash setup/root-setup.sh [username]   (default: bachman)
# Creates the unprivileged user that runs Bachman and owns its credentials, which a chat agent may only use
# through Bachman's narrow MCP tools. No sudo, no password, no SSH, home 700, linger.
set -euo pipefail
U=${1:-bachman}
[ "$(id -u)" = 0 ] || { echo "ABORT: run with sudo"; exit 1; }
id "$U" >/dev/null 2>&1 && { echo "ABORT: user $U already exists"; exit 1; }

adduser --disabled-password --gecos "Bachman podcast tools" "$U"
passwd -l "$U" >/dev/null
chmod 700 "/home/$U"
loginctl enable-linger "$U"

# TEMPORARY, lets the admin (and an assistant working as the admin) act as this user. Does NOT grant root.
# Remove when the project is finished: sudo rm /etc/sudoers.d/92-admin-as-$U
T=$(mktemp)
echo "${SUDO_USER:?run with sudo} ALL=($U) NOPASSWD: ALL" > "$T"
visudo -cf "$T"
install -m 440 -o root -g root "$T" "/etc/sudoers.d/92-admin-as-$U"
rm -f "$T"

echo "=== user:";   id "$U"
echo "=== home:";   ls -ld "/home/$U"
echo "=== linger:"; loginctl show-user "$U" -p Linger
echo "=== groups must NOT include sudo:"; id -nG "$U"
if sudo -u "$U" sudo -n true 2>/dev/null; then echo "FAIL: $U has sudo"; exit 1; else echo "ok: $U has no sudo"; fi
echo "=== ssh AllowUsers (must not list $U):"; sshd -T | grep -i '^allowusers'
