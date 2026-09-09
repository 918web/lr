# VIORA STUDIO 6.0.0

Правило рабочего пространства для Antigravity. Действует всегда, когда агент работает
в этом каталоге: посты в Telegram-канал VIORA STUDIO, ресерч интернета, разбор видео.
Содержание один в один с `.cursor/rules/viora-studio.mdc`: две среды, одни правила.

## Один источник правды

Скилл лежит в одном месте: папка `viora-studio` в корне проекта.
В `.claude/skills`, `.agents/skills` и `.cursor/skills` лежат только заглушки с указателем.
Заглушки не правим никогда: их перезаписывает `tools/install.py`.
Память канала, профиль автора и напоминания живут только в `viora-studio/memory/`.

## Шаг минус один: профиль автора

До любой работы, включая первый ответ автору:

```bash
cd viora-studio
python3 tools/memory.py profile missing
```

Скрипт напечатал пустые поля: задай ВСЕ вопросы ОДНИМ сообщением, запиши ответы
через `profile set` и больше не спрашивай никогда:

```bash
python3 tools/memory.py profile set город=Астрахань
python3 tools/memory.py profile show
```

Напечатало «профиль заполнен»: молчишь и работаешь.

## Правило ноль

Длинных тире нет нигде. Запрещены em dash U+2014, en dash U+2013,
horizontal bar U+2015, figure dash U+2012 и minus sign U+2212, причём и в постах,
и в статьях, и в коммитах, и в обычных ответах в чате. Ставим обычный дефис,
точку, запятую или двоеточие. Это первое, что автор замечает в тексте от ИИ.

Проверить свою собственную реплику перед отправкой:

```bash
cd viora-studio
python3 tools/lint_chat.py --text "твой ответ"
```

## Что читать

- Маленькое окно или быстрая модель: `viora-studio/QUICKCARD.txt`, больше ничего.
- Обычная работа: `viora-studio/SKILL.md`, двадцать два правила и пайплайн.
- Сложная задача: `viora-studio/ROUTER.md`, дальше ровно один файл из `viora-studio/reference/`.
- Среда без файлов: вставь `viora-studio/PROMPT-CORE.txt` в первое сообщение.

Не держи все правила в голове. Скрипты держат правила, модель держит вкус.

## Железное

- Футер последней строкой, над ним один хэштег-рубрика.
- Приветствие до пяти слов, сразу за ним цифра или срок.
- Личная деталь обязательна: с какого раза завелось, что отвалилось.
- Концовка просит переслать, а не поставить реакцию.
- Ссылки только ярлыком: `[ТЫК](url)`. Голых URL нет.
- Выдуманных ссылок, цифр, сроков и опыта автора не бывает. Нет в сырье - спрашиваем.
- Пост без чистого `lint_post.py --strict` автору не отдаём.
- После чистого линтера идёт проход-правка: `viora-studio/reference/edit-pass.md`.
- Публикует автор. Скилл только отдаёт текст.

## Команды

Все команды из каталога скилла: сначала `cd viora-studio`.
На Windows вместо `python3` пишем `python` или `py -3`.

```bash
# план по задаче: рубрика, каркас, коридор, один файл и ближайший эталон
python3 tools/viora.py route "нужен пост про абузы сервисов"

# автоправка, линтер, оценка и разрез одним ходом
python3 tools/viora.py ship draft.txt --rubric абуз

# шесть вариантов первой строки
python3 tools/viora.py hook "тема поста" --rubric абуз

python3 tools/lint_post.py draft.txt --strict
python3 tools/score_post.py draft.txt
python3 tools/split_post.py draft.txt --check
python3 tools/memory.py check-greeting "Здарова, любители халявы"
python3 tools/memory.py profile missing
python3 tools/research.py "запрос"
python3 tools/watch.py "https://youtu.be/ID"
python3 tools/viora.py doctor
python3 tools/validate_skill.py
python3 tools/run_evals.py
python3 tools/package.py --check
bash tools/check_all.sh
```

## Повседневное и расписание

```bash
python3 tools/life.py pc
python3 tools/life.py weather
python3 tools/life.py morning
python3 tools/schedule.py list
python3 tools/schedule.py add "08:00 morning"
```

Сеть тут только на чтение и без ключей. Ничего не отправляем сами.

## Телеграм

Соединение держит один процесс, сторож `tools/tg_watch.py`. Заявки автора берём
только через `python3 tools/tg.py next --wait 600 --json`, Избранное мимо моста
не читаем. Отправляем только командами моста: `stage --send`, `ask --send`, `publish`.
Бытовое (пк, погода, утро, напоминания, решение по фото) сторож отвечает сам.
В канал уходит только то, что автор подтвердил словом. Папку `secrets/` не открываем.
Подробности: `viora-studio/reference/telegram.md`, настройка: `TELEGRAM-SETUP.md`.

## Установка в свой проект

```bash
cd viora-studio
python3 tools/install.py --target /путь/до/проекта --dry-run
python3 tools/install.py --target /путь/до/проекта
python3 tools/install.py --global
```

По умолчанию в `.claude/skills`, `.agents/skills` и `.cursor/skills` ложатся заглушки
с относительным указателем. Флаг `--global` ставит заглушки с абсолютным путём в
`~/.claude/skills`, `~/.agents/skills`, `~/.cursor/skills` и `~/.gemini/config/skills`,
последний каталог как раз для Antigravity. Полная копия ставится флагом `--copy`
и нужна только там, где агент не может дойти до канонической папки по пути.
Чужие скиллы в этих папках установщик не трогает.
