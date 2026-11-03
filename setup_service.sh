#!/usr/bin/env bash
# Sets up LivingCode. Run it again at any time to update and restart everything.
#
# On this Mac it builds the local lab around the throwaway sample app.
# On Linux it creates the two users, the folders and the services. The Linux part has
# never been run, and the lab lock inside the code refuses to act on Linux for now, so
# treat it as a draft until it has been tried on a throwaway VM.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
export PYTHONPATH="$ROOT/src"

mac_lab() {
  if [[ ! -f "$ROOT/lab/etc/sample.toml" ]]; then
    "$PY" -m livingcode init
  fi
  "$PY" -m livingcode install
  "$PY" -m livingcode lab restart
  "$PY" -m livingcode doctor
  echo
  echo "Lab is ready. Try: PYTHONPATH=src $PY -m livingcode status"
  echo "Logs are in lab/var/log, and each file is size limited."
}

linux_services() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run this with sudo on Linux." >&2
    exit 1
  fi
  if [[ "${LIVINGCODE_I_KNOW_THIS_IS_UNTESTED:-}" != "yes" ]]; then
    echo "The Linux setup has never been run. Set LIVINGCODE_I_KNOW_THIS_IS_UNTESTED=yes to try it on a throwaway VM." >&2
    exit 1
  fi

  local app="sample"
  local state="/var/lib/livingcode/$app"

  id lc-guard >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin lc-guard
  id lc-agent >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin lc-agent

  # The guard and agent each get their own environment, away from the app's.
  install -d -o root -g root -m 755 /opt/livingcode /etc/livingcode
  "$PY" -m venv /opt/livingcode/guard-env
  /opt/livingcode/guard-env/bin/pip install --quiet --upgrade "$ROOT"
  "$PY" -m venv /opt/livingcode/agent-env
  /opt/livingcode/agent-env/bin/pip install --quiet --upgrade "$ROOT"

  # Only the guard can write the release, its database and the backups.
  install -d -o lc-guard -g lc-guard -m 750 "/opt/$app" "$state/guard"
  install -d -o lc-guard -g lc-agent -m 750 "$state/reports"
  # The agent can only write its own folder, and drop candidates and proposals.
  install -d -o lc-agent -g lc-agent -m 750 "$state/agent" "$state/proposals"
  install -d -o lc-agent -g lc-guard -m 770 "$state/candidates"
  install -d -o lc-guard -g lc-guard -m 750 /var/log/livingcode

  # The config is root owned, so the agent can read it but never change it.
  if [[ ! -f "/etc/livingcode/$app.toml" ]]; then
    echo "Write /etc/livingcode/$app.toml first (owned by root, mode 644), with enabled = false." >&2
    exit 1
  fi
  chown root:root "/etc/livingcode/$app.toml"
  chmod 644 "/etc/livingcode/$app.toml"
  if [[ -f "/etc/livingcode/$app.agent.env" ]]; then
    chown root:lc-agent "/etc/livingcode/$app.agent.env"
    chmod 640 "/etc/livingcode/$app.agent.env"
  fi

  cat > /etc/systemd/system/livingcode-guard.service <<EOF
[Unit]
Description=LivingCode guard (no AI in here)
After=network.target

[Service]
User=lc-guard
Environment=LIVINGCODE_CONFIG=/etc/livingcode/$app.toml
ExecStart=/opt/livingcode/guard-env/bin/livingcode guard-service
Restart=always
RestartSec=2
NoNewPrivileges=no

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/livingcode-agent.service <<EOF
[Unit]
Description=LivingCode agent
After=network.target livingcode-guard.service

[Service]
User=lc-agent
Environment=LIVINGCODE_CONFIG=/etc/livingcode/$app.toml
ExecStart=/opt/livingcode/agent-env/bin/livingcode agent-service
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$state/agent $state/candidates $state/proposals
CPUQuota=50%
MemoryMax=1G

[Install]
WantedBy=multi-user.target
EOF

  # The guard may restart the app and nothing else.
  cat > /etc/sudoers.d/livingcode-guard <<EOF
lc-guard ALL=(root) NOPASSWD: /usr/bin/systemctl restart $app.service, /usr/bin/systemctl start $app.service, /usr/bin/systemctl stop $app.service
EOF
  chmod 440 /etc/sudoers.d/livingcode-guard
  visudo -cf /etc/sudoers.d/livingcode-guard

  systemctl daemon-reload
  systemctl enable --now livingcode-guard.service livingcode-agent.service
  systemctl restart livingcode-guard.service livingcode-agent.service
  echo "Done. Follow the logs with: journalctl -u livingcode-guard.service -f"
}

case "$(uname -s)" in
  Darwin) mac_lab ;;
  Linux) linux_services ;;
  *) echo "Unsupported system" >&2; exit 1 ;;
esac
