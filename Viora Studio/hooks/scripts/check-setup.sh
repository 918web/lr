#!/usr/bin/env bash
# Короткая проверка на старте сессии. Молчит, когда всё в порядке.
# Говорит только о том, что может сломать работу скилла.
# Всегда выходит с кодом 0: сессию не блокируем никогда.

set -u

root="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$root" ]; then
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
skill="$root/viora-studio"

warn=""

if ! command -v python3 >/dev/null 2>&1; then
  warn="VIORA STUDIO: нет python3, скрипты tools не пойдут. Пиши без них по QUICKCARD.txt"
elif [ ! -f "$skill/QUICKCARD.txt" ] || [ ! -f "$skill/tools/viora.py" ]; then
  warn="VIORA STUDIO: папка скилла неполная в $skill. Переустанови: python3 tools/install.py --target ."
fi

if [ -n "$warn" ]; then
  echo "$warn"
fi

exit 0
