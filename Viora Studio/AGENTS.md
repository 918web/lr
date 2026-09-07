# VIORA STUDIO - точка входа для агента

Это рабочая папка бренда VIORA STUDIO. Здесь лежит скилл, который пишет посты
в Telegram-канал автора, ресерчит интернет и смотрит видео.

Версия скилла: 6.0.0. Язык работы: русский.

## ПРАВИЛО НОЛЬ. Читай раньше всего остального

**Длинных тире не бывает. Ни в постах, ни в статьях, ни в твоих ответах в чате.**

Запрещённые символы: `—` (em dash, U+2014), `–` (en dash, U+2013), `―` (horizontal bar, U+2015), <!-- dash-demo -->
`‒` (figure dash, U+2012), `−` (minus sign, U+2212). <!-- dash-demo -->

Вместо них: обычный дефис `-`, точка, запятая, двоеточие или новое предложение.
Это правило действует всегда: в посте, в статье, в коммите, в комментарии к коду
и в обычном разговоре с автором. Длинное тире - самая заметная подпись ИИ
в русском тексте, и автор её видит первой.

Проверить свой собственный ответ перед отправкой:

```bash
cd viora-studio
echo "текст ответа" | python3 tools/lint_chat.py
```

## Шаг минус один. Профиль автора

В новом чате, до любой работы:

```bash
cd viora-studio
python3 tools/memory.py profile missing
```

Код 1 значит: обязательные поля пусты. Спроси автора ВСЕ пустые поля одним
сообщением: город, часовой пояс, время подъёма, устройство. Запиши ответы
командой `python3 tools/memory.py profile set ключ=значение` и больше не спрашивай
никогда. Профиль лежит в `viora-studio/memory/profile.md` и один на все среды:
Claude Code, Codex, Cursor, Antigravity.

## Шаг 1. Выбери свой тир и не читай лишнего

Скилл собран так, чтобы не забивать контекстное окно. Читаешь ровно один файл
из таблицы, дальше по ссылкам внутри него.

| Кто ты | Что читаешь | Размер |
|---|---|---|
| Слабая или быстрая модель, маленькое окно, режим автодополнения | `viora-studio/QUICKCARD.txt` | 15 КБ |
| Обычная модель, есть доступ к файлам | `viora-studio/SKILL.md` | 27 КБ |
| Сложная задача, нужны детали | `SKILL.md`, дальше по `viora-studio/ROUTER.md` | по одному файлу |
| Среда без файловой системы, только чат | вставить `viora-studio/PROMPT-CORE.txt` | 30 КБ |

Признак того, что тебе хватит QUICKCARD: ты не уверена, что удержишь двадцать
правил одновременно. Тогда не держи их. Держи семь и гоняй линтер, он знает всё
остальное и говорит, что именно править.

## Шаг 2. Что вообще просят

| Задача автора | Куда идти |
|---|---|
| Написать пост в канал | `viora-studio/SKILL.md`, пайплайн из одиннадцати шагов |
| Не понятно, в каком порядке идут блоки текста | `viora-studio/reference/frameworks.md` |
| Посты похожи один на другой, всегда список | `viora-studio/reference/matrix.md` |
| Пост правильный, но его никто не пересылает | `viora-studio/reference/mirror.md` |
| Пост получился длинный, 10+ пунктов | `viora-studio/reference/split.md` |
| Собрать статью на telegra.ph | `viora-studio/reference/telegraph.md` |
| Найти инфу в интернете, проверить сервис, посмотреть чужие посты | `viora-studio/viora-research/SKILL.md` |
| Посмотреть видео на YouTube, разобрать чужой ролик | `viora-studio/viora-video/SKILL.md` |
| Сделать пост красивее, разобрать вёрстку | `viora-studio/reference/beauty.md` |
| Убрать ИИ-слова, вычистить текст | `viora-studio/reference/antislop.md` |
| Настроить канал, ключи, описание | `viora-studio/reference/seo.md` |
| Собрать шортс | `viora-studio/reference/shorts.md` |
| Понять, что уже выходило | `viora-studio/memory/post-log.md`, `posts/INDEX.md` |
| Понять, что зашло по цифрам | `viora-studio/memory/metrics.md` |

## Шаг 3. Команды

Все команды запускаются из каталога скилла: сначала `cd viora-studio`,
дальше ровно так, как написано ниже. Так же написаны команды во всех
файлах скилла, чтобы их можно было копировать без правки. Если забыла сделать
cd, точка входа сама напомнит одной строкой и всё равно сработает.
Питон только стандартный, ставить ничего не нужно.
На Windows питон зовётся `python` или `py -3`: если `python3` не нашёлся,
подставь своё имя во всех командах ниже. Каким именем звать питон здесь,
скажет `python3 tools/viora.py doctor`.

```bash
# главная команда номер один: план по задаче
python3 tools/viora.py route "нужен пост про абузы сервисов"

# главная команда номер два: автоправка, линтер, оценка и разрез сразу
python3 tools/viora.py ship draft.txt --rubric абуз

# шесть вариантов первой строки под тему и рубрику
python3 tools/viora.py hook "Notion Business без карты" --rubric абуз

# починить только механику вёрстки и перезаписать файл
python3 tools/lint_post.py draft.txt --fix --rubric абуз

# поставить посту оценку на цифрах канала
python3 tools/score_post.py draft.txt

# посмотреть память канала и проверить приветствие на повтор
python3 tools/memory.py show
python3 tools/memory.py check-greeting "Здарова, любители халявы"

# проверить черновик поста
python3 tools/lint_post.py draft.txt --strict

# то же машинно, с подсказками как править
python3 tools/lint_post.py draft.txt --json

# нужно ли резать пост на пост плюс статью
python3 tools/split_post.py draft.txt --check

# порезать: получить пост для Telegram и заготовку статьи
python3 tools/split_post.py draft.txt --out /tmp/viora

# проверить свой ответ в чате на ИИ-слова и длинные тире
python3 tools/lint_chat.py --text "твой ответ"

# ресерч по интернету, сжатая выжимка
python3 tools/research.py "что искать"

# посмотреть видео, транскрипт без кадров
python3 tools/watch.py "https://youtu.be/ID"

# комп, погода, утро и напоминания без модели
python3 tools/life.py pc
python3 tools/life.py weather
python3 tools/schedule.py list

# смена в Телеграме: следующая заявка автора и состояние моста
python3 tools/tg.py next --wait 600 --json
python3 tools/tg.py doctor

# поставить скилл в свой проект: заглушка в .claude, .agents и .cursor
python3 tools/install.py --target /путь/до/проекта --dry-run

# проверить упаковку скилла по спеке Agent Skills
python3 tools/validate_skill.py

# прогнать 32 эталона поведения
python3 tools/run_evals.py

# проверить среду: питон, файлы, память, сеть, yt-dlp
python3 tools/viora.py doctor

# собрать поставку в dist с заглушками в папках обнаружения
python3 tools/package.py --check

# полный прогон перед тем, как считать скилл рабочим
bash tools/check_all.sh
```

## Шаг 4. Что нельзя никогда

1. Выдумывать ссылки, домены, цифры, сроки и опыт автора. Нет в сырье - спрашиваем одной строкой.
2. Ставить длинные тире. Смотри правило ноль.
3. Отдавать пост без футера и без хэштега-рубрики.
4. Писать в первой строке приветствие длиннее пяти слов.
5. Заканчивать пост риторическим вопросом. Заканчиваем просьбой переслать.
6. Отдавать пост, который не прошёл `lint_post.py --strict`.
7. Публиковать что-либо самому. Скилл отдаёт текст автору, публикует автор.

## Структура папки

```
Viora Studio/
  AGENTS.md                    этот файл, точка входа
  CLAUDE.md GEMINI.md          то же для Claude Code и Gemini
  .cursorrules .cursor/rules/  то же для Cursor
  .agents/rules/               то же для Antigravity
  .claude/skills/ .agents/skills/ .cursor/skills/   заглушки с указателем на viora-studio
  README.md                    человеческое описание
  TELEGRAM-SETUP.md            Телеграм за три шага
  start-watch.ps1 start-watch.sh   запуск смены, QR, doctor, autostart
  secrets/                     ключи и строка сессии, агенту читать запрещено
  posts/                       архив вышедших постов и индекс
  viora-studio/
    QUICKCARD.txt              тир 0: одна карточка на всё
    SKILL.md                   тир 1: пайплайн и правила
    ROUTER.md                  что читать под какую задачу
    PROMPT-CORE.txt            ядро для среды без файлов
    PROMPT-FULL.txt            всё одним файлом
    reference/                 тир 2: детали по одной теме на файл
    viora-research/            интернет-ресерч
    viora-video/               просмотр видео
    memory/                    цифры, решения, профиль автора, напоминания
    evals/                     эталоны поведения и фикстуры
    tools/                     линтеры, резчик, ресерч, видео, мост, сторож, агент, сборка
```
