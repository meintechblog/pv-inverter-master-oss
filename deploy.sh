#!/usr/bin/env bash
# Deploy pv-inverter-master to an LXC container (set LXC_HOST=user@host)
# Usage: ./deploy.sh [--first-time]
set -euo pipefail

# NOTE (Phase 43+): After install.sh runs the blue-green migration,
# /opt/pv-inverter-master is a symlink pointing to
# /opt/pv-inverter-master-releases/current -> /opt/pv-inverter-master-releases/<release>/.
# rsync follows the destination symlink and writes into the real release
# directory, so this script continues to work unchanged on migrated hosts.
#
# For a FIRST-TIME bootstrap on a new LXC, run install.sh on the LXC
# instead of --first-time here. --first-time is kept for backwards compat
# but no longer creates /opt/pv-inverter-master as a plain directory.

LXC_HOST="${LXC_HOST:-root@192.0.2.10}"
REMOTE_DIR="/opt/pv-inverter-master"
SERVICE="pv-inverter-master"

echo "=== Deploying pv-inverter-master to $LXC_HOST ==="

# First-time setup (creates user, venv, apt deps — NOT the install dir)
if [[ "${1:-}" == "--first-time" ]]; then
    echo ">>> First-time setup..."
    echo "    NOTE (Phase 43+): Consider running install.sh on the LXC instead"
    echo "    for a clean blue-green bootstrap:"
    echo "      ssh $LXC_HOST 'curl -fsSL https://raw.githubusercontent.com/meintechblog/pv-inverter-master-oss/main/install.sh | bash'"
    echo ""

    ssh "$LXC_HOST" bash -s <<'SETUP'
set -euo pipefail

# Create service user (no login)
id pv-proxy &>/dev/null || useradd -r -s /usr/sbin/nologin pv-proxy

# Create config dir only — do NOT mkdir /opt/pv-inverter-master, which would
# interfere with the Phase 43+ blue-green layout (where install_dir is a
# symlink managed by install.sh).
mkdir -p /etc/pv-inverter-master

# Install Python + venv + rsync
apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip git rsync

chown -R pv-proxy:pv-proxy /etc/pv-inverter-master

echo ">>> First-time prereqs done."
echo ">>> Run install.sh next to create the blue-green layout, then re-run deploy.sh without --first-time."
SETUP
    echo ""
    echo "First-time prereqs installed on $LXC_HOST."
    echo "Next step: ssh $LXC_HOST and run install.sh, then re-run ./deploy.sh (no flags)."
    exit 0
fi

# Sync source code (exclude dev files, .planning, tests, .git*)
# NOTE: We exclude both `.git/` (directory in main checkouts) and `.git`
# (file pointer in git worktrees). Without the file exclude, a deploy from
# a worktree would ship the dangling gitdir pointer to the LXC, breaking
# install.sh migration's `git describe` call with a nosha/v0.0 fallback name.
# Phase 44 CHECK-01: Capture short SHA from the dev-side git checkout into a
# COMMIT file that ships with the sync. On the LXC, `.git/` is excluded for
# size/security reasons, so `git rev-parse` cannot run there. The updater's
# get_commit_hash() falls back to reading this file. We write it into the
# source tree briefly, sync it, then remove it so the working tree stays clean.
COMMIT_SHORT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "$COMMIT_SHORT" > src/pv_inverter_master/COMMIT
trap 'rm -f src/pv_inverter_master/COMMIT' EXIT

echo ">>> Syncing source code (commit=$COMMIT_SHORT)..."
rsync -avz --delete \
    --exclude '.git/' \
    --exclude '.git' \
    --exclude '.gitignore' \
    --exclude 'tests/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.venv/' \
    --exclude 'node_modules/' \
    --exclude '.pytest_cache/' \
    --exclude '.*/' \
    ./ "$LXC_HOST:$REMOTE_DIR/"

# Install package + copy service files
echo ">>> Installing package..."
ssh "$LXC_HOST" bash -s <<'INSTALL'
set -euo pipefail
cd /opt/pv-inverter-master
.venv/bin/pip install -e . --quiet

# Update systemd units (main + recovery + updater, Phase 43+ / 45-04).
# The recovery + updater units are copied even if this is pre-migration —
# the files are harmless until install.sh enables them, and having them
# in place means the next install.sh run has no unit-file copy work left.
cp config/pv-inverter-master.service /etc/systemd/system/
if [ -f config/pv-inverter-master-recovery.service ]; then
    cp config/pv-inverter-master-recovery.service /etc/systemd/system/
fi
# Phase 45-04 privileged updater path+service pair
if [ -f config/pv-inverter-master-updater.path ]; then
    cp config/pv-inverter-master-updater.path /etc/systemd/system/
fi
if [ -f config/pv-inverter-master-updater.service ]; then
    cp config/pv-inverter-master-updater.service /etc/systemd/system/
fi
# Hourly anomaly detector oneshot + timer (quick-260529-jrc).
if [ -f config/pv-inverter-anomaly.service ]; then
    cp config/pv-inverter-anomaly.service /etc/systemd/system/
fi
if [ -f config/pv-inverter-anomaly.timer ]; then
    cp config/pv-inverter-anomaly.timer /etc/systemd/system/
fi
systemctl daemon-reload
# Enable recovery unit if present (idempotent; safe on pre-migration hosts).
if [ -f /etc/systemd/system/pv-inverter-master-recovery.service ]; then
    systemctl enable pv-inverter-master-recovery.service 2>/dev/null || true
fi
# Phase 45-04: enable + start the updater .path watcher (idempotent).
# The .service has no [Install] and is only activated by the .path.
if [ -f /etc/systemd/system/pv-inverter-master-updater.path ]; then
    systemctl enable pv-inverter-master-updater.path 2>/dev/null || true
    systemctl restart pv-inverter-master-updater.path 2>/dev/null || \
        systemctl start pv-inverter-master-updater.path 2>/dev/null || true
fi
# quick-260529-jrc: enable + (re)start the hourly anomaly detector timer.
# Daylight gating is in-script, so firing year-round hourly is fine. The
# .service has no [Install] — the timer owns scheduling. Idempotent.
if [ -f /etc/systemd/system/pv-inverter-anomaly.timer ]; then
    systemctl enable pv-inverter-anomaly.timer 2>/dev/null || true
    systemctl restart pv-inverter-anomaly.timer 2>/dev/null || \
        systemctl start pv-inverter-anomaly.timer 2>/dev/null || true
fi
INSTALL

# Restart service
echo ">>> Restarting service..."
ssh "$LXC_HOST" "systemctl restart $SERVICE"

# Wait and check status
sleep 2
echo ">>> Service status:"
ssh "$LXC_HOST" "systemctl status $SERVICE --no-pager -l" || true

echo ""
echo "=== Deploy complete ==="
echo "Dashboard: http://${LXC_HOST#*@}"
echo "Logs:      ssh $LXC_HOST journalctl -u $SERVICE -f"
