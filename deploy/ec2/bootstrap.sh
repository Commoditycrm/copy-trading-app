#!/usr/bin/env bash
# EC2 user-data for the copy-trading prod host (Ubuntu 24.04, x86_64).
# Idempotent: safe to re-run by hand over SSM.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
exec > >(tee -a /var/log/ct-bootstrap.log) 2>&1

apt-get update
apt-get -y full-upgrade
apt-get install -y unattended-upgrades ca-certificates curl gnupg jq unzip chrony

# security updates only auto-reboot Sunday 08:00 UTC (US market closed)
cat > /etc/apt/apt.conf.d/52ct-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "08:00";
EOF
timedatectl set-timezone UTC

# 4 GiB swap: prod had 67 memcg OOM kills in Sep with none configured
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
cat > /etc/sysctl.d/99-ct.conf <<'EOF'
vm.swappiness=10
fs.inotify.max_user_watches=524288
EOF
sysctl --system >/dev/null

# Docker Engine + compose plugin
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
# bounded logs (Lightsail db log hit 1.1 GB) and a capped BuildKit cache (39 GB there)
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" },
  "live-restore": true,
  "builder": { "gc": { "enabled": true, "defaultKeepStorage": "10GB" } }
}
EOF
systemctl enable docker
systemctl restart docker

# AWS CLI v2
if ! command -v aws >/dev/null; then
  curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
  unzip -q -o /tmp/awscliv2.zip -d /tmp && /tmp/aws/install --update
fi

# CloudWatch agent; config is installed separately before it is started
if ! dpkg -s amazon-cloudwatch-agent >/dev/null 2>&1; then
  curl -fsSL https://s3.us-east-2.amazonaws.com/amazoncloudwatch-agent-us-east-2/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb -o /tmp/cwagent.deb
  dpkg -i /tmp/cwagent.deb
fi

# app account; docker group is root-equivalent, so access is via SSM only
id deploy >/dev/null 2>&1 || useradd -m -s /bin/bash deploy
usermod -aG docker deploy
install -d -o deploy -g deploy -m 750 /opt/copy-trading-app

# key-only SSH; prefer Session Manager and keep port 22 closed in the SG
cat > /etc/ssh/sshd_config.d/99-ct-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
MaxAuthTries 3
EOF
systemctl reload ssh || systemctl reload sshd || true

echo "ct-bootstrap complete $(date -u +%FT%TZ)"
