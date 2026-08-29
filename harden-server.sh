#!/bin/bash
# Run ONCE on a fresh IONOS VPS, as root, before deploying anything.
#
# The provisioning password is emailed in plaintext and the box is on a public
# IP that gets scanned within minutes, so this closes password auth entirely and
# moves you to keys. Have your public key ready — you will be locked out without it.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then echo "Run as root."; exit 1; fi

read -rp "Paste your SSH PUBLIC key (ssh-ed25519 AAAA...): " PUBKEY
if [[ ! "$PUBKEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-) ]]; then
  echo "That does not look like a public key. Aborting."; exit 1
fi

echo "==> Updating packages"
apt-get update -qq && apt-get upgrade -y -qq
apt-get install -y -qq ufw fail2ban unattended-upgrades python3-venv python3-pip git curl

echo "==> Creating 'platform' user"
if ! id platform &>/dev/null; then
  adduser --disabled-password --gecos "" platform
  usermod -aG sudo platform
fi

# --disabled-password means the account has no password, and sudo cannot
# authenticate an account with no password — so plain `usermod -aG sudo` leaves
# it unable to run anything privileged. Grant passwordless sudo instead, the
# same arrangement every cloud image ships with (SSH is key-only by the time
# this runs, so the key is the credential either way).
cat > /etc/sudoers.d/90-platform <<'SUDOEOF'
platform ALL=(ALL) NOPASSWD:ALL
SUDOEOF
chmod 440 /etc/sudoers.d/90-platform
visudo -c -f /etc/sudoers.d/90-platform
install -d -m 700 -o platform -g platform /home/platform/.ssh
echo "$PUBKEY" > /home/platform/.ssh/authorized_keys
chown platform:platform /home/platform/.ssh/authorized_keys
chmod 600 /home/platform/.ssh/authorized_keys

echo "==> Locking down SSH (key-only, no root login)"
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'SSHEOF'
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
X11Forwarding no
MaxAuthTries 3
SSHEOF
sshd -t && systemctl reload ssh

echo "==> Firewall: 22/80/443 only"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable

echo "==> fail2ban + unattended upgrades"
cat > /etc/fail2ban/jail.local <<'F2BEOF'
[sshd]
enabled = true
# Deliberately lenient: root login is disabled below, so every reflexive
# `ssh root@host` counts as a failure. A tight limit here locks the operator
# out of their own box during setup, which is worse than the attack it stops —
# key-only auth is what actually protects this, not the ban threshold.
maxretry = 8
bantime = 600
findtime = 600
F2BEOF
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

echo
echo "==> Done."
echo
echo "    BEFORE closing this session, open a NEW terminal and confirm:"
echo "      ssh platform@$(hostname -I | awk '{print $1}')"
echo
echo "    Log in as 'platform' from now on — NOT root. Root SSH is disabled, so"
echo "    each attempt is a failed auth and fail2ban will eventually ban you."
echo "    If you do get locked out: IONOS panel -> Remote Console -> log in as"
echo "    root -> fail2ban-client set sshd unbanip <your-ip>"
