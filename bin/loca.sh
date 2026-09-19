#!/bin/sh
# loca — локальный агент для кода поверх Ollama.
# Обёртка: запускает пакет прямо из репозитория, так что правки в нём видны сразу
# и не нужен pip install. Переопределяется переменными LOCA_REPO / LOCA_PYTHON.
LOCA_REPO="${LOCA_REPO:-$HOME/Downloads/loca}"
if [ ! -d "$LOCA_REPO/loca" ]; then
  echo "loca: не найден репозиторий в $LOCA_REPO (задайте LOCA_REPO=/путь/к/loca)" >&2
  exit 1
fi
PYTHONPATH="$LOCA_REPO${PYTHONPATH:+:$PYTHONPATH}" exec "${LOCA_PYTHON:-python3}" -m loca "$@"
