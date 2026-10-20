#!/bin/sh
# loca — локальный агент для кода поверх Ollama.
# Обёртка: запускает пакет прямо из репозитория, так что правки в нём видны сразу
# и не нужен pip install. Переопределяется переменными LOCA_REPO / LOCA_PYTHON.
SELF="$0"
while [ -L "$SELF" ]; do                          # ~/.local/bin/loca — обычно ссылка сюда
  LINK=$(readlink "$SELF"); case "$LINK" in /*) SELF="$LINK" ;; *) SELF="$(dirname "$SELF")/$LINK" ;; esac
done
GUESS="$(cd "$(dirname "$SELF")/.." 2>/dev/null && pwd)"
[ -d "$GUESS/loca" ] || GUESS="$HOME/Developer/loca"
LOCA_REPO="${LOCA_REPO:-$GUESS}"
if [ ! -d "$LOCA_REPO/loca" ]; then
  echo "loca: не найден репозиторий в $LOCA_REPO (задайте LOCA_REPO=/путь/к/loca)" >&2
  exit 1
fi
PYTHONPATH="$LOCA_REPO${PYTHONPATH:+:$PYTHONPATH}" exec "${LOCA_PYTHON:-python3}" -m loca "$@"
