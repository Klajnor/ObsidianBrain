#!/usr/bin/env bash
# Установка systemd-юнитов Brain: копирование в /etc/systemd/system и daemon-reload.
#
# Использование (от root):
#   sudo ./install-systemd.sh            # все юниты
#   sudo ./install-systemd.sh sync       # только brain-sync.service
#   sudo ./install-systemd.sh pull       # brain-pull.service + brain-pull.timer
#
# Скрипт только копирует файлы и делает daemon-reload. Юниты не включаются и не
# запускаются: варианты sync и pull взаимоисключающие, включайте один из них.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEST="/etc/systemd/system"
mode="${1:-all}"

usage() {
    cat <<'EOF'
Использование: sudo ./install-systemd.sh [sync|pull|all]

  sync  — brain-sync.service (демон sync.py --watch)
  pull  — brain-pull.service + brain-pull.timer (раз в 5 минут)
  all   — все юниты (по умолчанию)

После установки включите один вариант:
  sudo systemctl enable --now brain-sync
  sudo systemctl enable --now brain-pull.timer
EOF
}

if [[ $# -gt 1 ]]; then
    usage >&2
    exit 2
fi

case "$mode" in
    -h|--help|help)
        usage
        exit 0
        ;;
    all|--all)
        units=(brain-sync.service brain-pull.service brain-pull.timer)
        ;;
    sync|--sync)
        units=(brain-sync.service)
        ;;
    pull|--pull)
        units=(brain-pull.service brain-pull.timer)
        ;;
    *)
        echo "Неизвестный аргумент: $mode" >&2
        usage >&2
        exit 2
        ;;
esac

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Нужны права root: sudo $0 $*" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl не найден: скрипт рассчитан на Linux с systemd" >&2
    exit 1
fi

if [[ ! -d "$DEST" ]]; then
    echo "Нет каталога $DEST: systemd установлен нестандартно?" >&2
    exit 1
fi

for unit in "${units[@]}"; do
    if [[ ! -f "$SCRIPT_DIR/$unit" ]]; then
        echo "Нет файла $SCRIPT_DIR/$unit" >&2
        exit 1
    fi
done

for unit in "${units[@]}"; do
    install -m 0644 -o root -g root "$SCRIPT_DIR/$unit" "$DEST/$unit"
    # На SELinux-системах вернуть стандартный контекст (если restorecon есть).
    if command -v restorecon >/dev/null 2>&1; then
        restorecon -F "$DEST/$unit" 2>/dev/null || true
    fi
    echo "Установлен: $DEST/$unit"
done

systemctl daemon-reload
echo "systemd daemon-reload выполнен."

case "$mode" in
    sync|--sync)
        echo "Дальше: sudo systemctl enable --now brain-sync"
        ;;
    pull|--pull)
        echo "Дальше: sudo systemctl enable --now brain-pull.timer"
        ;;
    all|--all)
        echo "Выберите один вариант:"
        echo "  sudo systemctl enable --now brain-sync"
        echo "  sudo systemctl enable --now brain-pull.timer"
        ;;
esac
