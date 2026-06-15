#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запустіть через sudo: sudo ./uninstall.sh" >&2
    exit 1
fi

systemctl disable --now multiwan-manager.service 2>/dev/null || true
rm -f /etc/systemd/system/multiwan-manager.service
systemctl daemon-reload

echo "Сервіс видалено. Програма та конфіг залишені в /opt/multiwan-manager."
echo "Повне видалення: sudo rm -rf /opt/multiwan-manager /etc/default/multiwan-manager"
