#!/usr/bin/env bash
# Sets up LivingCode. Run it again at any time to update and restart everything.
#
# On this Mac it builds the local lab around the throwaway sample app.
#
# The web demo on a shared Linux VM, behind an nginx you already run (NGINX_SNIPPET turns
# this mode on):
#
#   sudo SERVICE_NAME=idea-livingcode PORT=8606 BASE_PATH=/livingcode/ \
#        NGINX_SNIPPET=/etc/nginx/max.d/livingcode.conf ./setup_service.sh
#
#   It installs nothing: no apt-get, no pip, no Caddy, no firewall, no certbot. It needs
#   python3 (3.11 or newer), nginx, systemd and curl to be there already. It writes three
#   systemd units, the nginx location file and one rate limit file in /etc/nginx/conf.d/,
#   checks nginx -t, and takes both nginx files out again if the check fails. Settings and
#   keys go in .env, which is kept when you run this again. The server block must include
#   the snippet folder (for example: include /etc/nginx/max.d/*.conf;).
#   Logs: journalctl -u idea-livingcode -f, plus a size limited copy in data/logs/.
#   RENDER_TO=<folder> only writes the units and nginx files into that folder, to look at.
#
# On Linux without NGINX_SNIPPET it creates the two users, the folders and the services for
# a real install. That part has never been run, and the lab lock inside the code refuses
# it for now, so treat it as a draft until it has been tried on a throwaway VM.
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

fail() {
  echo "$1" >&2
  exit 1
}

owner_of() {
  stat -c %U "$1" 2>/dev/null || stat -f %Su "$1"
}

set_env() { # set_env KEY VALUE, adds the line or changes just that line
  if grep -q "^$1=" "$ROOT/.env"; then
    sed -i "s|^$1=.*|$1=$2|" "$ROOT/.env"
  else
    echo "$1=$2" >> "$ROOT/.env"
  fi
}

env_value() { # env_value KEY DEFAULT
  local value
  value="$(grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
  echo "${value:-$2}"
}

# Hardening both units share. The runner adds no network and a much smaller view on top.
hardening() {
  cat <<UNIT
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
AmbientCapabilities=
SystemCallArchitectures=native
SystemCallFilter=@system-service
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UNIT
}

web_demo_vm() {
  local render="${RENDER_TO:-}"
  local service="${SERVICE_NAME:-idea-livingcode}"
  local port="${PORT:-8606}"
  local base="${BASE_PATH:-/livingcode/}"
  local snippet="$NGINX_SNIPPET"
  local zone_file="/etc/nginx/conf.d/${service}-limits.conf"
  local units=/etc/systemd/system
  local zone="${service//-/_}"
  local sock="/run/${service}/runner.sock"

  [[ -n "$render" || "${EUID}" -eq 0 ]] || fail "Run this with sudo."
  [[ "$service" =~ ^[a-z0-9][a-z0-9-]*$ ]] || fail "SERVICE_NAME may only use small letters, digits and dashes."
  [[ "$port" =~ ^[0-9]+$ ]] || fail "PORT must be a number."
  [[ "$base" == /*/ ]] || fail "BASE_PATH must start and end with a slash, like /livingcode/"
  [[ "$snippet" == /* ]] || fail "NGINX_SNIPPET must be a full path, like /etc/nginx/max.d/livingcode.conf"
  local relay="$ROOT/data/sock/relay.sock"
  (( ${#relay} < 100 )) || fail "The project folder's path is too long for the relay socket ($relay). Move it somewhere shorter."

  local py
  py="$(command -v python3 || true)"
  [[ -n "$py" ]] || fail "python3 is missing. This mode installs nothing, so add Python 3.11 or newer first."
  "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || fail "$("$py" -V) is too old. LivingCode needs Python 3.11 or newer, and this mode installs nothing."
  if [[ -z "$render" ]]; then
    local tool
    for tool in nginx systemctl curl; do
      command -v "$tool" >/dev/null || fail "$tool is missing. This mode installs nothing, so add it first."
    done
    grep -qE 'include\s+/etc/nginx/conf\.d/\*\.conf' /etc/nginx/nginx.conf \
      || fail "nginx.conf doesn't include conf.d, so the rate limit zone can't go there. Nothing changed."
  fi

  # Both units run as whoever owns the project folder, never as root.
  local run_user run_group
  run_user="${SERVICE_USER:-$(owner_of "$ROOT")}"
  [[ "$run_user" != root ]] || fail "The project folder belongs to root. Clone it as a normal user, so nothing here runs as root."
  run_group="$(id -gn "$run_user")"

  if [[ -n "$render" ]]; then
    mkdir -p "$render"
    units="$render"
    zone_file="$render/${service}-limits.conf"
    snippet="$render/$(basename "$snippet")"
  else
    if [[ -d "$ROOT/.git" && "${GIT_PULL:-true}" == true ]]; then
      echo "==> Latest code"
      # Fast forward only, so local changes are never thrown away. GIT_PULL=false skips it.
      sudo -u "$run_user" -H git -C "$ROOT" pull --ff-only || echo "    couldn't fast forward, using what's here"
    fi
    echo "==> Settings"
    if [[ ! -f "$ROOT/.env" ]]; then
      cp "$ROOT/.env.example" "$ROOT/.env"
      echo "    made .env from .env.example. It runs on the scripted fake AI until you add the Azure settings."
    fi
    set_env PORT "$port"
    # Behind the local nginx, only it is trusted for the visitor's address.
    set_env TRUST_PROXY 127.0.0.1
    chown "$run_user:$run_group" "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    install -d -o "$run_user" -g "$run_group" -m 700 "$ROOT/data" "$ROOT/data/state" "$ROOT/data/sock" "$ROOT/data/logs"
  fi
  local timeout
  timeout="$(env_value RUN_TIMEOUT_SECONDS 300)"
  [[ "$timeout" =~ ^[0-9]+$ ]] || fail "RUN_TIMEOUT_SECONDS in .env must be a number."

  echo "==> systemd units for ${service}"
  # The front: the page, the limits and the Azure key. It never runs the AI's code.
  cat > "$units/${service}.service" <<UNIT
[Unit]
Description=LivingCode web demo (${service})
After=network-online.target ${service}-runner.socket
Wants=network-online.target ${service}-runner.socket

[Service]
Type=simple
User=${run_user}
Group=${run_group}
WorkingDirectory=${ROOT}
EnvironmentFile=${ROOT}/.env
Environment=PYTHONPATH=${ROOT}/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
Environment=DATA_DIR=${ROOT}/data LOG_DIR=${ROOT}/data/logs LIVINGCODE_RUNNER_SOCKET=${sock}
ExecStart=${py} -m livingcode.webdemo.front
Restart=always
RestartSec=3
$(hardening)
ProtectHome=read-only
ReadWritePaths=${ROOT}/data
MemoryMax=512M
TasksMax=256

[Install]
WantedBy=multi-user.target
UNIT

  # One run per connection, one connection at a time.
  cat > "$units/${service}-runner.socket" <<UNIT
[Unit]
Description=LivingCode web demo runs (${service})

[Socket]
ListenStream=${sock}
Accept=yes
MaxConnections=1
SocketUser=${run_user}
SocketGroup=${run_group}
SocketMode=0600
DirectoryMode=0755
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
UNIT

  # A run: no network at all, nothing writable but its own private /tmp, which goes away
  # with it. It sees the project read only, without .env, the front's state or the logs.
  cat > "$units/${service}-runner@.service" <<UNIT
[Unit]
Description=LivingCode web demo run with no network (${service})
CollectMode=inactive-or-failed

[Service]
Type=simple
User=${run_user}
Group=${run_group}
StandardInput=socket
StandardOutput=socket
StandardError=journal
WorkingDirectory=/tmp
Environment=PYTHONPATH=${ROOT}/src PYTHONDONTWRITEBYTECODE=1 HOME=/tmp TMPDIR=/tmp LC_ALL=C.UTF-8
Environment=LIVINGCODE_CONFINED=1 LIVINGCODE_RELAY_SOCKET=${ROOT}/data/sock/relay.sock
ExecStart=${py} -m livingcode.webdemo.runner
RuntimeMaxSec=$((timeout + 20))
$(hardening)
PrivateNetwork=yes
PrivateIPC=yes
ProtectHome=tmpfs
BindReadOnlyPaths=${ROOT}
InaccessiblePaths=-${ROOT}/.env -${ROOT}/data/state -${ROOT}/data/logs
MemoryMax=768M
TasksMax=64
CPUQuota=100%
LimitNOFILE=1024
UNIT

  if [[ -z "$render" ]]; then
    systemctl daemon-reload
    systemctl enable "${service}-runner.socket"
    systemctl restart "${service}-runner.socket"
    systemctl enable "${service}.service"
    systemctl restart "${service}.service"
  fi

  echo "==> nginx: ${base} -> 127.0.0.1:${port}"
  local had_snippet=false had_zone=false
  if [[ -z "$render" ]]; then
    mkdir -p "$(dirname "$snippet")"
    if [[ -f "$snippet" ]]; then cp "$snippet" "$snippet.bak"; had_snippet=true; fi
    if [[ -f "$zone_file" ]]; then cp "$zone_file" "$zone_file.bak"; had_zone=true; fi
  fi
  cat > "$zone_file" <<NGINX
# ${service}: rate limit zones, used by ${NGINX_SNIPPET}
limit_req_zone \$binary_remote_addr zone=${zone}:10m rate=5r/s;
limit_conn_zone \$binary_remote_addr zone=${zone}_conn:10m;
NGINX
  cat > "$snippet" <<NGINX
# ${service}: written by setup_service.sh in ${ROOT}
location = ${base%/} { return 301 ${base}; }
location ${base} {
    limit_req zone=${zone} burst=30 nodelay;
    limit_req_status 429;
    limit_conn ${zone}_conn 8;
    limit_conn_status 429;
    client_max_body_size 4k;
    proxy_pass http://127.0.0.1:${port}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$remote_addr;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header Connection "";
    # The run comes in as a long lived event stream
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 1h;
}
NGINX
  if [[ -n "$render" ]]; then
    echo "Wrote the files to $render. Nothing else changed."
    return
  fi
  if nginx -t 2>"/tmp/${service}-nginx-test.log"; then
    rm -f "$snippet.bak" "$zone_file.bak"
    systemctl reload nginx
  else
    cat "/tmp/${service}-nginx-test.log" >&2
    if $had_snippet; then mv "$snippet.bak" "$snippet"; else rm -f "$snippet"; fi
    if $had_zone; then mv "$zone_file.bak" "$zone_file"; else rm -f "$zone_file"; fi
    fail "nginx -t failed, so both nginx files were put back the way they were."
  fi

  echo "==> Check"
  local i
  for i in $(seq 1 15); do
    if curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1; then
      systemctl is-active --quiet "${service}-runner.socket" || fail "The page is up, but ${service}-runner.socket isn't. Look at: journalctl -u ${service}-runner.socket -n 50"
      echo "    up at ${base}. Logs: journalctl -u ${service} -f"
      echo "    each run on its own: journalctl -u '${service}-runner@*' -f"
      return
    fi
    sleep 1
  done
  fail "The service didn't come up. Look at: journalctl -u ${service} -n 50"
}

if [[ -n "${NGINX_SNIPPET:-}" ]]; then
  [[ "$(uname -s)" == Linux || -n "${RENDER_TO:-}" ]] || fail "The shared VM mode is for Linux."
  web_demo_vm
  exit 0
fi

case "$(uname -s)" in
  Darwin) mac_lab ;;
  Linux) linux_services ;;
  *) echo "Unsupported system" >&2; exit 1 ;;
esac
