#!/usr/bin/env bash
# Reproduce this box's hardening on a fresh host.
#
# Everything here was applied to the live box by hand first and is checked in
# so the next one does not have to be. Ordered so nothing can lock you out:
# firewall rules are staged before the firewall is enabled, and sshd config is
# validated before sshd is restarted.
#
#   sudo ./deploy/harden.sh [admin-ip]
#
# `admin-ip` is whitelisted in fail2ban so a misfiring jail cannot ban you off
# your own box. Defaults to the address of the SSH session running this.
set -euo pipefail
cd "$(dirname "$0")"

ADMIN_IP="${1:-$(echo "${SSH_CLIENT:-}" | awk '{print $1}')}"
[ -n "$ADMIN_IP" ] && echo "==> whitelisting $ADMIN_IP" || echo "!!  no admin IP; fail2ban may ban you"

echo "==> firewall (rules staged before enabling)"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw limit 22/tcp  comment 'ssh (rate limited)' >/dev/null
ufw allow 80/tcp  comment 'http - certbot + redirect' >/dev/null
ufw allow 443/tcp comment 'https' >/dev/null
ufw --force enable >/dev/null
ufw status | sed 's/^/    /'

echo "==> fail2ban"
apt-get install -y -qq fail2ban >/dev/null
install -m 644 fail2ban-jail.local /etc/fail2ban/jail.local
if [ -n "$ADMIN_IP" ]; then
  sed -i "s|^ignoreip .*|ignoreip  = 127.0.0.1/8 ::1 $ADMIN_IP|" /etc/fail2ban/jail.local
fi
systemctl enable --now fail2ban >/dev/null
fail2ban-client reload >/dev/null
fail2ban-client status | sed 's/^/    /'

echo "==> sshd"
install -m 644 sshd-hardening.conf /etc/ssh/sshd_config.d/99-hardening.conf
# Validate before restarting: a bad config that only fails on restart is how
# you lose a box you cannot get a console on.
sshd -t
systemctl restart ssh
sshd -T | grep -E '^(permitrootlogin|passwordauthentication|x11forwarding|allowtcpforwarding)' | sed 's/^/    /'

echo "==> patches"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get -y \
  -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef \
  full-upgrade >/dev/null
apt-get -y -qq autoremove >/dev/null
echo "    $(apt list --upgradable 2>/dev/null | grep -c 'upgradable from') packages still upgradable"

echo "==> backups"
( crontab -l 2>/dev/null | grep -v 'deploy/backup.sh'; \
  echo '15 3 * * * /opt/content-forge/deploy/backup.sh >> /home/ubuntu/backups/backup.log 2>&1' ) | crontab -

echo
echo "Done. The app must also bind loopback — see content-forge.service."
[ -f /var/run/reboot-required ] && echo "A reboot is required to finish (new kernel/libc)."
