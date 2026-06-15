#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/multiwan-manager"
SERVICE_FILE="/etc/systemd/system/multiwan-manager.service"
ENV_FILE="/etc/default/multiwan-manager"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запустіть інсталятор через sudo: sudo ./install.sh" >&2
    exit 1
fi

install_packages() {
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y \
            python3 python3-dev python3-venv build-essential \
            iproute2 iputils-ping conntrack
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-devel gcc iproute iputils conntrack-tools
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 python3-devel gcc iproute iputils conntrack-tools
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --needed --noconfirm python base-devel iproute2 iputils conntrack-tools
    else
        echo "Пакетний менеджер не підтримується. Встановіть Python 3, venv, iproute2, ping і conntrack." >&2
        exit 1
    fi
}

install_packages

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 6) else 1)' || {
    echo "Потрібен Python 3.6 або новіший." >&2
    exit 1
}

systemctl stop multiwan-manager.service 2>/dev/null || true
install -d -m 0755 "${APP_DIR}/templates"

for file in app.py requirements.txt README.md; do
    install -m 0644 "${SOURCE_DIR}/${file}" "${APP_DIR}/${file}"
done
install -m 0644 "${SOURCE_DIR}/templates/index.html" "${APP_DIR}/templates/index.html"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install \
    --disable-pip-version-check --no-cache-dir --upgrade \
    "pip==21.3.1" "setuptools==58.5.3" "wheel==0.37.1"
"${APP_DIR}/.venv/bin/python" -m pip install \
    --disable-pip-version-check --no-cache-dir --prefer-binary \
    -r "${APP_DIR}/requirements.txt"

install -m 0644 "${SOURCE_DIR}/multiwan-manager.service" "${SERVICE_FILE}"
if [[ ! -f "${ENV_FILE}" ]]; then
    cat >"${ENV_FILE}" <<'EOF'
WAN_BIND=0.0.0.0
WAN_PORT=5000
WAN_THREADS=4
LOG_LEVEL=INFO
EOF
else
    sed -i '/^WAN_USERNAME=/d; /^WAN_PASSWORD=/d' "${ENV_FILE}"
fi
chmod 0644 "${ENV_FILE}"

systemctl daemon-reload
systemctl enable --now multiwan-manager.service

echo
echo "Multi-WAN Manager для Jatson встановлено."
echo "Статус: systemctl status multiwan-manager --no-pager"
echo "Логи:  journalctl -u multiwan-manager -n 50 --no-pager"
echo "Панель: http://IP_СЕРВЕРА:$(grep -E '^WAN_PORT=' "${ENV_FILE}" | cut -d= -f2 || echo 5000)"
