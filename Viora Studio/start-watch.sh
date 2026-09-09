#!/bin/sh
# VIORA STUDIO. Смена в Телеграме, один вход.
#
#   sh start-watch.sh              держать смену (первый раз покажет QR для входа)
#   sh start-watch.sh doctor       проверить доступы, сторожа, автозапуск, агента
#   sh start-watch.sh login        войти заново по QR
#   sh start-watch.sh login phone  войти заново по номеру и коду
#   sh start-watch.sh autostart    поставить автозапуск при входе в систему
#   sh start-watch.sh autostart remove   убрать автозапуск
#
# Сам находит Python, сам ставит telethon и qrcode, сам показывает QR один раз.
# Второй экземпляр при живом стороже не стартует и говорит почему.

here=$(cd "$(dirname "$0")" && pwd)
tools="$here/viora-studio/tools"

PYTHONIOENCODING=utf-8
PYTHONUTF8=1
VIORA_SECRETS="$here/secrets"
export PYTHONIOENCODING PYTHONUTF8 VIORA_SECRETS

env_file="$VIORA_SECRETS/watch.env"
if [ -f "$env_file" ]; then
    set -a
    . "$env_file"
    set +a
fi

py=""
for name in python3 python py; do
    if command -v "$name" >/dev/null 2>&1; then
        if "$name" -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' >/dev/null 2>&1; then
            py="$name"
            break
        fi
    fi
done
if [ -z "$py" ]; then
    echo "Не нашёл Python 3. Поставь его и запусти файл снова."
    exit 1
fi

if [ ! -f "$tools/tg.py" ]; then
    echo "Рядом нет папки viora-studio/tools. Положи этот файл в корень поставки."
    exit 1
fi

mode="serve"
sub=""
keep=""
for a in "$@"; do
    key=$(printf '%s' "$a" | sed 's#^[-/]*##' | tr 'A-Z' 'a-z')
    case "$key" in
        doctor) mode="doctor" ;;
        login) mode="login" ;;
        setup) mode="setup" ;;
        autostart) mode="autostart" ;;
        phone|remove|status|install) sub="$key" ;;
        *) keep="$keep $a" ;;
    esac
done

if [ "$mode" = "doctor" ]; then
    "$py" "$tools/tg.py" doctor
    echo ""
    exec "$py" "$tools/tg_watch.py" --doctor
fi

if [ "$mode" = "login" ]; then
    if [ "$sub" = "phone" ]; then
        exec "$py" "$tools/tg.py" login --force --phone
    fi
    exec "$py" "$tools/tg.py" login --force
fi

if [ "$mode" = "autostart" ]; then
    action="install"
    if [ -n "$sub" ] && [ "$sub" != "phone" ]; then
        action="$sub"
    fi
    if [ "$action" = "install" ]; then
        # Автозапуск без входа бесполезен: сначала убеждаемся, что сессия есть.
        if ! "$py" "$tools/tg.py" setup; then
            echo ""
            echo "Сначала доведи вход до конца, потом ставь автозапуск."
            exit 1
        fi
    fi
    exec "$py" "$tools/autostart.py" "$action"
fi

if ! "$py" "$tools/tg.py" setup; then
    echo ""
    echo "Подключение не доведено до конца. В строках выше сказано, чего не хватает."
    exit 1
fi
if [ "$mode" = "setup" ]; then
    exit 0
fi

# shellcheck disable=SC2086
exec "$py" "$tools/tg_watch.py" $keep
