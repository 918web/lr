#!/usr/bin/env bash
# Один прогон перед тем, как считать скилл рабочим.
# Запуск: bash tools/check_all.sh
set -u

# Кэш питона ломает валидатор упаковки и уезжает в поставку. Проще не создавать.
export PYTHONDONTWRITEBYTECODE=1

cd "$(dirname "$0")/.." || exit 1
fail=0

echo "== тесты линтера постов =="
python3 tools/lint_post.py --selftest || fail=1
echo
echo "== тесты линтера статьи =="
python3 tools/lint_article.py --selftest || fail=1
echo

echo "== тесты автоправки =="
python3 tools/lint_post.py --selftest-fix || fail=1
echo

echo "== тесты оценки поста =="
python3 tools/score_post.py --selftest || fail=1
echo

echo "== тесты памяти канала =="
python3 tools/memory.py --selftest || fail=1
echo

echo "== тесты линтера речи =="
python3 tools/lint_chat.py --selftest || fail=1
echo

echo "== тесты разреза поста =="
python3 tools/split_post.py --selftest || fail=1
echo

echo "== тесты точки входа =="
python3 tools/viora.py selftest-own || fail=1
echo

echo "== тесты таблиц размеров =="
python3 tools/sizes.py --selftest || fail=1
python3 tools/sizes.py --check || fail=1
echo

echo "== сборка промптов =="
python3 tools/build_prompt.py || fail=1
python3 tools/build_prompt.py --check || fail=1
echo

echo "== образец поста в строгом режиме =="
python3 tools/lint_post.py tools/sample-post.txt --strict || fail=1
echo

echo "== среда и зависимости =="
python3 tools/viora.py doctor --offline || fail=1
echo

echo "== эталоны поведения =="
python3 tools/run_evals.py || fail=1
echo

echo "== установка в чистую папку =="
probe="$(mktemp -d)"
python3 tools/install.py --target "$probe" --dry-run || fail=1
rm -rf "$probe"
echo

echo "== состав поставки =="
python3 tools/package.py --check || fail=1
echo

echo "== упаковка по спеке Agent Skills =="
# Сначала убираем кэш, иначе валидатор честно пожалуется на наш же мусор.
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
python3 tools/validate_skill.py || fail=1
echo

echo "== длинные тире во всём скилле =="
# Строки с меткой dash-demo это нарочные показы, что именно запрещено.
# Корень поставки проверяем целиком: README, TELEGRAM-SETUP, правила сред,
# скрипты запуска и secrets/README.txt тоже под правилом ноль.
python3 tools/validate_skill.py --dashes-only .. || fail=1
echo

echo "== тесты повседневных ответов =="
life_out=$(python3 tools/life.py --selftest) || fail=1
echo "$life_out" | tail -1
echo

echo "== тесты расписания напоминаний =="
sched_out=$(python3 tools/schedule.py --selftest) || fail=1
echo "$sched_out" | tail -1
echo

echo "== тесты моста с телегой =="
tg_out=$(python3 tools/tg.py selftest) || fail=1
echo "$tg_out" | tail -1
echo

echo "== тесты сторожа: outbox, медиа, lock, планировщик, мозг =="
watch_out=$(python3 tools/tg_watch.py --selftest) || fail=1
echo "$watch_out" | tail -1
echo

echo "== тесты агента как мозга =="
agent_out=$(python3 tools/agent.py --selftest) || fail=1
echo "$agent_out" | tail -1
echo

echo "== тесты автозапуска =="
auto_out=$(python3 tools/autostart.py --selftest) || fail=1
echo "$auto_out" | tail -1
echo

echo "== тесты нейронки и цепочки поста =="
ai_out=$(python3 tools/ai.py --selftest) || fail=1
echo "$ai_out" | tail -1
post_out=$(python3 tools/post.py --selftest) || fail=1
echo "$post_out" | tail -1
echo

echo "== доступность скриптов ресерча и видео =="
python3 tools/research.py --version || fail=1
python3 tools/watch.py --version || fail=1
echo "сеть и yt-dlp проверяются отдельно: python3 tools/research.py --doctor и python3 tools/watch.py --doctor"
echo

if [ "$fail" = "0" ]; then
	echo "ВСЁ ЧИСТО"
else
	echo "ЕСТЬ ПРОБЛЕМЫ, смотри выше"
fi

exit $fail
