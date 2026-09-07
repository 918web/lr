#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VIORA STUDIO: одна точка входа для всех инструментов скилла.

Слабая модель не обязана помнить восемь скриптов и их флаги.
Ей хватает одного файла и двух слов: route для плана, ship для проверки.

    python3 tools/viora.py route "нужен пост про абузы сервисов"
    python3 tools/viora.py ship draft.txt --rubric абуз

Остальное уходит в свои скрипты без изменений:
    fix, lint, score, split, chat, memory, research, video, life, schedule
    install, validate, evals, package, doctor, check, selftest
"""

import os
import subprocess
import sys

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
MIN_DEFAULT = 70

PASS_THROUGH = {
    "lint": "lint_post.py",
    "score": "score_post.py",
    "split": "split_post.py",
    "chat": "lint_chat.py",
    "memory": "memory.py",
    "research": "research.py",
    "video": "watch.py",
    "install": "install.py",
    "validate": "validate_skill.py",
    "evals": "run_evals.py",
    "package": "package.py",
    "tg": "tg.py",
    "agent": "agent.py",
    "autostart": "autostart.py",
    "life": "life.py",
    "schedule": "schedule.py",
}

SELFTESTS = (
    ("линтер постов", "lint_post.py", ["--selftest"]),
    ("автоправка", "lint_post.py", ["--selftest-fix"]),
    ("оценка поста", "score_post.py", ["--selftest"]),
    ("память канала", "memory.py", ["--selftest"]),
    ("линтер речи", "lint_chat.py", ["--selftest"]),
    ("разрез поста", "split_post.py", ["--selftest"]),
    ("установщик", "install.py", ["--selftest"]),
    ("валидатор упаковки", "validate_skill.py", ["--selftest"]),
    ("раннер эталонов", "run_evals.py", ["--selftest"]),
    ("сборщик поставки", "package.py", ["--selftest"]),
    ("повседневные ответы", "life.py", ["--selftest"]),
    ("расписание напоминаний", "schedule.py", ["--selftest"]),
    ("мост с телегой", "tg.py", ["--selftest"]),
    ("сторож избранного", "tg_watch.py", ["--selftest"]),
    ("агент как мозг", "agent.py", ["--selftest"]),
    ("автозапуск", "autostart.py", ["--selftest"]),
)

# рубрика: каркас, запасной каркас, коридор знаков, коридор пунктов, один файл
ROUTES = {
    "абуз": ("SLAY", "PAS", (700, 1400), (4, 7), "reference/formats.md"),
    "сервисы": ("SLAY", "BAB", (500, 900), (3, 5), "reference/formats.md"),
    "гайд": ("PAS", "AIDA", (900, 1800), None, "reference/formats.md"),
    "релиз": ("BAB", "STAR", (400, 700), (1, 3), "reference/github.md"),
    "девлог": ("STAR", "BAB", (500, 900), (1, 3), "reference/examples.md"),
    "личное": ("STAR", "PAS", (400, 900), None, "reference/voice.md"),
    "опрос": ("PAS", "AIDA", (200, 450), (2, 4), "reference/formats.md"),
    "шортс": ("AIDA", "BAB", (250, 500), (1, 1), "reference/shorts.md"),
    "лидмагнит": ("BAB", "AIDA", (400, 700), (1, 1), "reference/telegraph.md"),
    "неробит": ("STAR", "PAS", (250, 500), (1, 2), "reference/formats.md"),
}

# Слова, по которым видна рубрика. Порядок важен: берётся первое совпадение.
# «абуз перестал работать» должен попасть в неробит, а не в абуз.
RUBRIC_WORDS = (
    ("неробит", ("неробит", "не работает", "перестал работать", "сломал", "отвал", "забанил")),
    ("абуз", ("абуз", "халяв", "бесплатн", "промокод", "триал", "кредит")),
    ("лидмагнит", ("лидмагнит", "лид-магнит", "большой материал", "мой опыт", "путь от")),
    ("гайд", ("гайд", "инструкц", "как сделать", "как получить", "туториал", "пошагов")),
    ("сервисы", ("сервис", "подборк", "пак ", "тулз", "инструмент", "нейронк", "модел")),
    ("релиз", ("релиз", "выпустил", "выложил проект", "готов проект", "репозитор")),
    ("девлог", ("девлог", "дневник", "пилю", "разработк", "прогресс", "билд")),
    ("шортс", ("шортс", "shorts", "ролик", "видос на канал")),
    ("опрос", ("опрос", "голосован", "что выберете", "спросить подписчик")),
    ("личное", ("личн", "мысли", "выгорел", "итоги", "про себя")),
)

# Слова задачи, которые важнее рубрики: они прямо называют нужный файл.
TASK_FILES = (
    (("крючок", "первая строка", "первый экран", "хук", "заголовок поста"), "reference/hook.md"),
    (("тире", "ии-текст", "нейросетев", "слоп", "вычист", "живее", "по-человечес"), "reference/antislop.md"),
    (("телеграф", "telegraph", "статью", "статья"), "reference/telegraph.md"),
    (("резать", "разрезать", "делить", "не влезает", "слишком длинн"), "reference/split.md"),
    (("seo", "поисков", "сниппет", "title", "слаг"), "reference/seo.md"),
    (("каркас", "структур", "pas", "aida", "bab", "star", "slay"), "reference/frameworks.md"),
    (("приветств", "здарова", "начало поста"), "reference/greetings.md"),
    (("красот", "оформлен", "верстк", "эмодзи", "жирн"), "reference/beauty.md"),
    (("слова", "замен", "синоним", "лексик"), "reference/words.md"),
    (("голос", "тон", "стиль автора"), "reference/voice.md"),
    (("кроспост", "дзен", "хабр", "твиттер"), "reference/crosspost.md"),
    (("пример", "образец", "как было"), "reference/examples.md"),
    (("github", "гитхаб", "репозитор"), "reference/github.md"),
    (("ресерч", "research", "найди в интернете", "проверь факт", "источник", "актуальн"), "viora-research/LITE.md"),
    (("видео", "youtube", "ютуб", "транскрипт", "таймкод", "перескажи"), "viora-video/LITE.md"),
)


def norm(text):
    return " ".join((text or "").lower().replace("ё", "е").split())


def detect_rubric(task):
    """Рубрика по словам задачи. Без хэштега, только живая речь автора."""
    low = norm(task)
    for rubric, words in RUBRIC_WORDS:
        for word in words:
            if word.replace("ё", "е") in low:
                return rubric
    return None


def pick_file(task, rubric):
    """Ровно один файл на чтение. Слова задачи важнее рубрики."""
    low = norm(task)
    for words, path in TASK_FILES:
        for word in words:
            if word.replace("ё", "е") in low:
                return path, "по словам задачи"
    if rubric and rubric in ROUTES:
        return ROUTES[rubric][4], "по рубрике"
    return "reference/formats.md", "по умолчанию"


def index_spots():
    """Где искать индекс эталонов. Посты лежат рядом со скиллом, а не внутри.

    Собственная папка проверяется первой на случай, если автор перенесёт
    посты внутрь скилла. Текущий каталог последним: так работает запуск из репозитория.
    """
    return (os.path.join(SKILL, "posts", "INDEX.md"),
            os.path.join(os.path.dirname(SKILL), "posts", "INDEX.md"),
            os.path.join(os.getcwd(), "posts", "INDEX.md"))


def index_text():
    """Тело posts/INDEX.md или пустая строка, если индекса рядом нет."""
    for spot in index_spots():
        try:
            with open(spot, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            continue
    return ""


def index_rows_text(text):
    """Строки таблицы индекса: (название, файл поста, рубрика)."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("| ["):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        title = cells[0]
        name = ""
        if "](" in title:
            head, tail = title.split("](", 1)
            title = head.lstrip("[").strip()
            name = tail.rstrip(")").strip()
        rows.append((title, name, cells[1].lstrip("#").strip()))
    return rows


def index_rows(text=None):
    """Разобранный индекс. Без аргумента читает файл с диска."""
    return index_rows_text(index_text() if text is None else text)


def example_line(rubric, text=None):
    """Одна строка про ближайший эталон той же рубрики.

    Читать эталон надо ДО написания, иначе пост пишется по памяти модели,
    а не по голосу канала. Индекса может не быть: тогда ведём в examples.md.
    """
    rows = index_rows(text)
    if not rows:
        return ("Эталон рубрики: индекса posts/INDEX.md рядом нет, "
                "читай reference/examples.md до написания")
    for title, name, mark in rows:
        if rubric and mark == rubric and name:
            return ("Эталон рубрики #%s: posts/%s (%s), прочитай до написания"
                    % (rubric, name, title))
    return ("Эталона этой рубрики в posts/INDEX.md нет, "
            "читай reference/examples.md до написания")


def route_text(task):
    """Собрать план в строки. Отдельно от печати, чтобы проверять тестом."""
    rubric = detect_rubric(task)
    path, why = pick_file(task, rubric)
    short = (task or "").strip()
    if len(short) > 120:
        short = short[:117] + "..."
    rows = ["ЗАДАЧА: %s" % short]

    if rubric:
        frame, backup, length, items, _ = ROUTES[rubric]
        rows.append("Рубрика: #%s" % rubric)
        rows.append("Каркас: %s, запасной %s" % (frame, backup))
        corridor = "Коридор: %d-%d знаков" % length
        if items:
            corridor += ", пунктов от %d до %d" % items
        rows.append(corridor)
    else:
        rows.append("Рубрика: не определилась, спроси автора одним вопросом")
        rows.append("Каркас: PAS для боли, SLAY для списка")
        rows.append("Коридор: держись 500-900 знаков, пока рубрика не ясна")

    rows.append("Читай ровно один файл (%s): %s" % (why, path))
    rows.append(example_line(rubric))
    rows.append("Первый экран: 90-100 знаков, внутри цифра или выгода")
    rows.append("Хвост: концовка, хэштег отдельной строкой, футер последней строкой")
    rows.append("")
    rows.append("Порядок работы:")
    rows.append("  1. python3 tools/memory.py check-greeting \"твоё приветствие\"")
    rows.append("  2. пишешь угол одним предложением: reference/edit-pass.md")
    rows.append("  3. пишешь черновик в draft.txt")
    if rubric:
        rows.append("  4. python3 tools/viora.py ship draft.txt --rubric %s" % rubric)
    else:
        rows.append("  4. python3 tools/viora.py ship draft.txt")
    rows.append("  5. правишь то, что осталось после автоправки, и гонишь ship снова")
    rows.append("  6. проход-правка по reference/edit-pass.md, потом ship последний раз")
    return rows, rubric, path


def take_brand(argv):
    """Вытащить --brand из аргументов. По умолчанию vs, то есть бренд V.S."""
    brand = "vs"
    rest = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--brand" and index + 1 < len(argv):
            brand = argv[index + 1].strip().lower()
            index += 2
            continue
        if item.startswith("--brand="):
            brand = item.split("=", 1)[1].strip().lower()
            index += 1
            continue
        rest.append(item)
        index += 1
    if brand not in ("vs", "nerobit"):
        brand = "vs"
    return brand, rest


def route(task, brand="vs"):
    if brand == "nerobit":
        print("Бренд: НЕ РОБИТ, мемы про код и ИИ")
        print("Читаешь РОВНО один файл: reference/nerobit.md")
        print("")
        print("Порядок:")
        print('  1. python3 tools/memory.py check-meme "шутка одной строкой"')
        print("  2. подпись 40-220 знаков, хэштег #мем, футера и оффера нет")
        print("  3. python3 tools/viora.py ship meme.txt --brand nerobit")
        print('  4. python3 tools/memory.py add-meme "шутка" --template когда')
        return 0
    if not (task or "").strip():
        print("Скажи задачу словами:")
        print('  python3 tools/viora.py route "пост про абузы сервисов"')
        return 2
    rows, _, _ = route_text(task)
    print("\n".join(rows))
    return 0


def ship(argv, brand="vs"):
    """Одна команда от черновика до вердикта: автоправка, линтер, оценка, разрез."""
    path = None
    rubric = None
    media = False
    article_path = None
    minimum = MIN_DEFAULT
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--rubric" and index + 1 < len(argv):
            rubric = argv[index + 1]
            index += 2
            continue
        if item.startswith("--rubric="):
            rubric = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--min" and index + 1 < len(argv):
            minimum = int(argv[index + 1])
            index += 2
            continue
        if item.startswith("--min="):
            minimum = int(item.split("=", 1)[1])
            index += 1
            continue
        if item == "--media":
            media = True
            index += 1
            continue
        if item == "--article" and index + 1 < len(argv):
            article_path = argv[index + 1]
            index += 2
            continue
        if item.startswith("--article="):
            article_path = item.split("=", 1)[1]
            index += 1
            continue
        if item.startswith("-"):
            print("Не знаю флаг: %s" % item)
            return 2
        path = item
        index += 1

    if not path:
        print("Дай файл черновика:")
        print("  python3 tools/viora.py ship draft.txt --rubric абуз")
        return 2
    if not os.path.exists(path):
        print("Нет такого файла: %s" % path)
        return 2

    sys.path.insert(0, HERE)
    import lint_article as A
    import lint_post as L
    import score_post as S
    import split_post as P

    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    print("ШАГ 1. АВТОПРАВКА")
    text, applied = L.autofix(text, rubric, media, brand)
    if applied:
        for item in applied:
            print("  - [%s] %s" % (item["code"], item["what"]))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("  файл перезаписан: %s" % path)
    else:
        print("  чинить было нечего")
    print("")

    print("ШАГ 2. ЛИНТЕР")
    issues, found = L.lint(text, rubric, media, brand)
    errors, warns = L.split_levels(issues, False)
    L.report(path, issues, found, False, False, brand)
    print("")

    if brand == "nerobit":
        print("ШАГ 3. ОЦЕНКА")
        print("  мем смотрит автор глазами: шкала V.S. на подпись 40-220 знаков не считается")
        print("")
        print("ШАГ 4. РАЗРЕЗ")
        print("  мем не режем: один ролик это одна шутка")
        print("")
        data = {"total": minimum, "fixes": []}
        cut = {"need_article": False, "reasons": []}
    else:
        print("ШАГ 3. ОЦЕНКА")
        data = S.score(text, rubric, media, SKILL, brand)
        S.print_report(path, data)
        print("")

        print("ШАГ 4. РАЗРЕЗ")
        cut = P.verdict(text, media)
        for reason in cut["reasons"]:
            print("  - %s" % reason)
        print("")

    # ШАГ 5. Статья лежит отдельным файлом, значит и линтер у неё свой.
    # Без этого шага телеграф смотрел только автор глазами.
    article_errors = []
    if article_path:
        print("ШАГ 5. СТАТЬЯ")
        if not os.path.exists(article_path):
            print("  нет такого файла: %s" % article_path)
            article_errors = [{"code": "E-A-EMPTY"}]
        else:
            with open(article_path, encoding="utf-8") as handle:
                article_text = handle.read()
            found_article = A.lint(strip_handoff(article_text))
            article_errors = [item for item in found_article
                              if item["level"] == "ERROR"]
            for item in found_article:
                mark = "ОШИБКА" if item["level"] == "ERROR" else "предупреждение"
                print("  [%s] %s: %s" % (mark, item["code"], item["message"]))
            if not found_article:
                print("  статья чистая")
        print("")

    ok = (not errors and not article_errors
          and data["total"] >= minimum and not cut["need_article"])
    print("ИТОГ")
    print("  ошибки линтера: %d" % len(errors))
    print("  предупреждения: %d" % len(warns))
    print("  оценка: %d из 100, порог %d" % (data["total"], minimum))
    print("  статья на телеграф: %s"
          % ("нужна" if cut["need_article"] else "не нужна"))
    if article_path:
        print("  ошибки статьи: %d" % len(article_errors))
    if ok:
        print("  ГОТОВО: пост можно отдавать автору")
        return 0

    print("  НЕ ГОТОВО. Дальше по порядку:")
    number = 1
    if errors:
        print("    %d. Почини ошибки линтера выше: механику скрипт уже сделал, остался смысл" % number)
        number += 1
    if article_errors:
        print("    %d. Почини ошибки статьи выше: подпись V.Studio, ### на пункты, без очевидностей" % number)
        number += 1
    if data["total"] < minimum:
        for fix in data["fixes"]:
            print("    %d. %s" % (number, fix["what"]))
            print("       почему: %s" % fix["why"])
            number += 1
    if cut["need_article"]:
        print("    %d. python3 tools/viora.py split %s%s"
              % (number, path, " --media" if media else ""))
        number += 1
    print("    %d. python3 tools/viora.py ship %s%s"
          % (number, path, " --rubric %s" % rubric if rubric else ""))
    return 1


ARTICLE_HEAD = "=== СТАТЬЯ НА TELEGRA.PH ==="
HANDOFF_STOPS = (
    "=== ПОСТ В ТГК ===",
    "=== ПОСТ В TELEGRAM ===",
    "=== ПОДПИСЬ К МЕДИА ===",
    "=== КОММЕНТАРИЙ ПЕРВЫМ ===",
    "=== СЦЕНАРИЙ ШОРТСА ===",
)


def strip_handoff(text):
    """Из файла сдачи вынуть только кусок статьи.

    Разрез отдаёт статью и пост в одном файле под шапками. Если отдать
    такой файл линтеру статьи целиком, футер поста читается как хвост
    статьи и вскакивает ложный E-A-SUBSCRIBE.
    """
    if ARTICLE_HEAD not in text:
        return text
    piece = text.split(ARTICLE_HEAD, 1)[1]
    for stop in HANDOFF_STOPS:
        if stop in piece:
            piece = piece.split(stop, 1)[0]
    return piece.strip()


def article(argv):
    """Линтер статьи для телеграфа. Понимает и файл сдачи с шапками."""
    paths = [item for item in argv if not item.startswith("-")]
    flags = [item for item in argv if item.startswith("-")]
    unknown = [item for item in flags if item not in ("--strict", "--json")]
    if unknown:
        print("Не знаю флаг: %s" % unknown[0])
        return 2
    if not paths:
        print("Дай файл статьи:")
        print("  python3 tools/viora.py article article.md")
        print("  python3 tools/viora.py article sdacha.txt --strict")
        return 2
    path = paths[0]
    if not os.path.exists(path):
        print("Нет такого файла: %s" % path)
        return 2

    sys.path.insert(0, HERE)
    import lint_article as A

    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    piece = strip_handoff(text)
    as_json = "--json" in flags
    if piece != text and not as_json:
        print("Взял кусок статьи из файла сдачи: %d знаков" % len(piece))
        print("")
    return A.report(A.lint(piece), "--strict" in flags, as_json)


HOOK_ORDER = ["дефицит", "цифра", "запрет", "провал", "спор", "пробел"]

HOOK_ARCHETYPES = {
    "дефицит": ("окно закрывается",
                "%s. Робит до конца недели, потом закроют."),
    "цифра": ("число вместо прилагательного",
              "%s: 3 попытки, 40 минут, один рабочий путь."),
    "запрет": ("так нельзя, но робит",
               "%s. Не по правилам, но у меня завелось."),
    "провал": ("у меня сломалось",
               "%s. С первого раза не завелось, помог один флаг."),
    "спор": ("все говорят иначе",
             "Говорят, %s уже не робит. Проверил сам."),
    "пробел": ("ты не знал, что это есть",
               "%s лежит у тебя под рукой, а ты не знал."),
}

HOOK_BY_RUBRIC = {
    "абуз": ("дефицит", "запрет"),
    "сервисы": ("цифра", "пробел"),
    "гайд": ("пробел", "провал"),
    "релиз": ("цифра", "провал"),
    "девлог": ("провал", "цифра"),
    "личное": ("провал", "спор"),
    "опрос": ("спор", "пробел"),
    "шортс": ("цифра", "дефицит"),
    "лидмагнит": ("пробел", "дефицит"),
    "неробит": ("запрет", "провал"),
}


def hook(argv, brand="vs"):
    """Дать шесть вариантов первой строки под тему и рубрику.

    Слабая модель чаще всего заваливает именно первый экран: пишет
    вступление вместо крючка. Здесь она получает готовые строки с именем
    архетипа и берёт первую. Разбор архетипов: reference/hook.md.
    """
    topic_words = []
    rubric = None
    take_rubric = False
    for item in argv:
        if take_rubric:
            rubric = norm(item)
            take_rubric = False
            continue
        if item == "--rubric":
            take_rubric = True
            continue
        if item.startswith("--"):
            continue
        topic_words.append(item)

    topic = " ".join(topic_words).strip()
    if brand == "nerobit" and topic:
        print("Тема: %s" % topic)
        print("Бренд: НЕ РОБИТ")
        print("")
        print("У мема крючок это текст на экране в первые 2 секунды, а не строка поста.")
        print("Восемь шаблонов с примерами: reference/nerobit.md")
        print("Хронометраж и кадр: reference/shorts.md, раздел про мем-шортс")
        print("")
        print("Дальше:")
        print('  python3 tools/memory.py check-meme "%s"' % topic)
        print("  python3 tools/viora.py ship meme.txt --brand nerobit")
        return 0

    if not topic:
        print("Скажи тему одной строкой, например:")
        print('  python3 tools/viora.py hook "Notion Business без карты" --rubric абуз')
        return 2

    if rubric not in HOOK_BY_RUBRIC:
        rubric = detect_rubric(topic) or "сервисы"
    first, backup = HOOK_BY_RUBRIC[rubric]

    print("Тема: %s" % topic)
    print("Рубрика: #%s" % rubric)
    print("Твой архетип: %s, запасной: %s" % (first, backup))
    print("")
    print("Шесть вариантов первого экрана. Первая строка твоя, остальные на выбор:")

    order = [first, backup]
    order += [name for name in HOOK_ORDER if name not in order]
    for name in order:
        why, template = HOOK_ARCHETYPES[name]
        line = template % topic
        mark = "   <- твой" if name == first else ""
        print("")
        print("[%s] %s%s" % (name, why, mark))
        print("  %s" % line)
        print("  знаков: %d, бюджет первого экрана 100" % len(line))

    print("")
    print("Дальше: поставил строку в начало черновика и прогнал")
    print("  python3 tools/viora.py ship draft.txt --rubric %s" % rubric)
    return 0


DOCTOR_FILES = ("SKILL.md", "QUICKCARD.txt", "ROUTER.md", "VERSION", "CHANGELOG.md",
                "PROMPT-CORE.txt", "PROMPT-FULL.txt", os.path.join("evals", "cases.json"))
DOCTOR_TOOLS = ("lint_post.py", "score_post.py", "split_post.py", "lint_chat.py",
                "memory.py", "research.py", "watch.py", "build_prompt.py", "sizes.py",
                "install.py", "validate_skill.py", "run_evals.py", "package.py",
                "check_all.sh", "tg.py", "tg_watch.py", "life.py",
                "schedule.py", "agent.py", "autostart.py")
DOCTOR_MEMORY = (os.path.join("memory", "metrics.md"), os.path.join("memory", "post-log.md"),
                 os.path.join("memory", "used-greetings.md"), os.path.join("memory", "decisions.md"))
# Эти строки не роняют прогон: без сети и yt-dlp посты пишутся как обычно.
DOCTOR_SOFT = ("сеть для ресерча", "yt-dlp для видео", "ffmpeg для видео",
               "имя питона")


def python_call(which=None):
    """Каким именем зовётся питон на этой машине и что нашлось в PATH.

    Вся документация написана через python3, а на Windows такого имени обычно нет:
    там работает python или лаунчер py -3. Автор должен видеть своё имя, а не чужое.
    """
    import shutil as _shutil

    finder = which or _shutil.which
    found = [name for name in ("python3", "python", "py") if finder(name)]
    if "python3" in found:
        return "python3", found
    if "python" in found:
        return "python", found
    if "py" in found:
        return "py -3", found
    return os.path.basename(sys.executable) or "python", found


def python_hint(which=None):
    """Строка для доктора: (чем звать, что нашлось, подсказка автору)."""
    call, found = python_call(which=which)
    # Сначала смотрим, нашлось ли вообще что-то в PATH: имя из sys.executable
    # тоже бывает python3, а звать его по имени всё равно нельзя.
    if not found:
        return call, found, ("в PATH нет ни python3, ни python, ни py: зови полным путём %s"
                             % call)
    if call == "python3":
        return call, found, "python3, команды из документации работают как есть"
    return call, found, ('python3 нет, пиши %s: %s tools/viora.py route "тема"'
                        % (call, call))


def doctor_rows(offline=False):
    """Строки таблицы среды: (что, ок, подробность, что делать)."""
    import shutil as _shutil

    rows = []
    ver = "%d.%d.%d" % sys.version_info[:3]
    ok = sys.version_info >= (3, 8)
    rows.append(("питон", ok, ver, "" if ok else "нужен python 3.8 или новее"))

    call, found, hint = python_hint()
    rows.append(("имя питона", bool(found), hint,
                 "" if found else "добавь питон в PATH или зови его полным путём"))

    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    ok = ("utf" in enc) or enc == ""
    rows.append(("кодировка вывода", ok, enc or "буфер без кодировки",
                 "" if ok else "поставь PYTHONIOENCODING=utf-8, иначе русский текст сломается"))

    gone = [n for n in DOCTOR_FILES if not os.path.exists(os.path.join(SKILL, n))]
    rows.append(("файлы скилла", not gone,
                 "все %d на месте" % len(DOCTOR_FILES) if not gone else "нет: " + ", ".join(gone),
                 "" if not gone else "собери промпты: python3 tools/build_prompt.py"))

    ref_dir = os.path.join(SKILL, "reference")
    refs = sorted(n for n in os.listdir(ref_dir) if n.endswith(".md")) if os.path.isdir(ref_dir) else []
    rows.append(("библиотека reference", len(refs) >= 15, "%d файлов" % len(refs),
                 "" if len(refs) >= 15 else "библиотека неполная, распакуй скилл целиком"))

    gone = [n for n in DOCTOR_TOOLS if not os.path.exists(os.path.join(HERE, n))]
    rows.append(("скрипты", not gone,
                 "все %d на месте" % len(DOCTOR_TOOLS) if not gone else "нет: " + ", ".join(gone),
                 "" if not gone else "распакуй скилл целиком"))

    subs = [n for n in ("viora-research", "viora-video") if os.path.isdir(os.path.join(SKILL, n))]
    rows.append(("под-скиллы", len(subs) == 2, ", ".join(subs) or "нет",
                 "" if len(subs) == 2 else "нет папки под-скилла"))

    thin = []
    for rel in DOCTOR_MEMORY:
        full = os.path.join(SKILL, rel)
        if not os.path.exists(full) or os.path.getsize(full) < 20:
            thin.append(rel)
    rows.append(("память канала", not thin, "заполнена" if not thin else "пусто: " + ", ".join(thin),
                 "" if not thin else "запиши цифры: python3 tools/memory.py add-metric"))

    if offline:
        rows.append(("сеть для ресерча", True, "проверка выключена флагом --offline", ""))
    else:
        import socket
        try:
            socket.create_connection(("duckduckgo.com", 443), timeout=3).close()
            rows.append(("сеть для ресерча", True, "есть", ""))
        except OSError as err:
            rows.append(("сеть для ресерча", False, "нет: %s" % err,
                         "ресерч и видео подождут, посты пишутся как обычно"))

    yt = _shutil.which("yt-dlp")
    rows.append(("yt-dlp для видео", bool(yt), yt or "нет",
                 "" if yt else "нужен только для tools/watch.py: pip install yt-dlp"))
    ff = _shutil.which("ffmpeg")
    rows.append(("ffmpeg для видео", bool(ff), ff or "нет",
                 "" if ff else "нужен только для кадров и звука в tools/watch.py"))
    return rows


def doctor(argv):
    """Одна таблица про среду и одна строка вердикта."""
    offline = "--offline" in argv
    as_json = "--json" in argv
    rows = doctor_rows(offline=offline)
    hard = [name for name, ok, _, _ in rows if not ok and name not in DOCTOR_SOFT]
    soft = [name for name, ok, _, _ in rows if not ok and name in DOCTOR_SOFT]

    if as_json:
        import json as _json
        print(_json.dumps({"version": VERSION, "skill": SKILL,
                           "rows": [{"name": n, "ok": ok, "detail": d, "fix": f}
                                    for n, ok, d, f in rows],
                           "blocking": hard, "optional": soft},
                          ensure_ascii=False, indent=2))
        return 1 if hard else 0

    width = max(len(name) for name, _, _, _ in rows)
    print("Среда для VIORA STUDIO, точка входа %s" % VERSION)
    print("Каталог скилла: %s" % SKILL)
    print("")
    for name, ok, detail, fix in rows:
        print("  %-*s  %s  %s" % (width, name, "ок " if ok else "нет", detail))
        if not ok and fix:
            print("  %-*s      %s" % (width, "", fix))
    print("")
    if hard:
        print("Посты писать нельзя, сначала почини: %s" % ", ".join(hard))
        return 1
    if soft:
        print("Посты пишем, ресерч и видео пока нет: %s" % ", ".join(soft))
        return 0
    print("Всё на месте, можно работать.")
    return 0


def run_script(script, args):
    # Наш вывод буферизуется, когда его читает конвейер или файл.
    # Без сброса заголовки секций встанут не по порядку и читатель запутается.
    sys.stdout.flush()
    return subprocess.call([sys.executable, os.path.join(HERE, script)] + list(args))


def check():
    sys.stdout.flush()
    return subprocess.call(["bash", os.path.join(HERE, "check_all.sh")])


def own_selftest():
    """Проверить саму точку входа: она решает, что модель прочитает и сделает."""
    import contextlib
    import io
    import shutil
    import tempfile

    checks = []
    checks.append(("абуз по словам", detect_rubric("нужен пост про абузы сервисов") == "абуз"))
    checks.append(("гайд по словам", detect_rubric("как получить доступ пошагово") == "гайд"))
    checks.append(("неробит важнее абуза", detect_rubric("абуз перестал работать") == "неробит"))
    checks.append(("шортс по словам", detect_rubric("сделай шортс на канал") == "шортс"))
    checks.append(("пусто без слов", detect_rubric("просто напиши что-нибудь") is None))

    path, why = pick_file("перепиши без тире, слишком по-нейросетевому", "абуз")
    checks.append(("слова задачи важнее рубрики",
                   path == "reference/antislop.md" and why == "по словам задачи"))
    path, why = pick_file("пост про абузы", "абуз")
    checks.append(("файл по рубрике",
                   path == "reference/formats.md" and why == "по рубрике"))
    path, _ = pick_file("", None)
    checks.append(("файл по умолчанию", path == "reference/formats.md"))

    rows, rubric, path = route_text("собери пак абузов на выходные")
    joined = "\n".join(rows)
    checks.append(("в плане есть каркас", "SLAY" in joined))
    checks.append(("в плане есть коридор", "700-1400" in joined))
    checks.append(("в плане ровно один файл",
                   joined.count("Читай ровно один файл") == 1))
    checks.append(("в плане есть ship", "viora.py ship" in joined))
    checks.append(("в плане есть угол", "угол одним предложением" in joined))
    checks.append(("в плане есть проход-правка", "проход-правка" in joined))
    checks.append(("в плане есть edit-pass", "reference/edit-pass.md" in joined))
    checks.append(("в плане есть эталон рубрики", "Эталон" in joined))

    sample_index = ("| Пост | Рубрика | Охват |\n"
                    "|---|---|---|\n"
                    "| [Морской бой](2026-08-morskoy-boy-s-ii.md) | релиз | - |\n"
                    "| [Крипта](2026-08-kripta-privatka.md) | личное | - |\n")
    sample_rows = index_rows(sample_index)
    checks.append(("индекс эталонов разобран", len(sample_rows) == 2))
    checks.append(("шапка таблицы не эталон", all(row[1] for row in sample_rows)))
    checks.append(("в индексе есть файл поста",
                   sample_rows[0][1] == "2026-08-morskoy-boy-s-ii.md"))
    checks.append(("в индексе есть рубрика", sample_rows[0][2] == "релиз"))
    same = example_line("релиз", sample_index)
    checks.append(("эталон рубрики нашёлся",
                   "posts/2026-08-morskoy-boy-s-ii.md" in same))
    checks.append(("эталон требует чтения до написания", "до написания" in same))
    checks.append(("чужая рубрика уходит в примеры",
                   "reference/examples.md" in example_line("шортс", sample_index)))
    checks.append(("пустой индекс не роняет план",
                   "reference/examples.md" in example_line("абуз", "")))

    for name in ("lint_post.py", "score_post.py", "split_post.py", "lint_chat.py",
                 "memory.py", "research.py", "watch.py", "check_all.sh",
                 "install.py", "validate_skill.py", "run_evals.py", "package.py",
                 "life.py", "schedule.py"):
        checks.append(("на месте %s" % name,
                       os.path.exists(os.path.join(HERE, name))))

    wanted = ([item[4] for item in ROUTES.values()]
              + ["viora-research/LITE.md", "viora-video/LITE.md",
                 "reference/edit-pass.md"])
    for name in wanted:
        checks.append(("на месте %s" % name,
                       os.path.exists(os.path.join(SKILL, name))))

    def quiet(argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ship(argv)
        return code, buffer.getvalue()

    tmp = tempfile.mkdtemp(prefix="viora-ship-")
    try:
        good = os.path.join(tmp, "good.txt")
        shutil.copyfile(os.path.join(HERE, "sample-post.txt"), good)
        code, _ = quiet([good])
        checks.append(("образец проходит ship", code == 0))

        dirty = os.path.join(tmp, "dirty.txt")
        with open(dirty, "w", encoding="utf-8") as handle:
            handle.write("Здарова \u2014 вот сервисы!!!\n\u2022 \u2460 Diagram \u2192 схемы\n")
        code, _ = quiet([dirty, "--rubric", "сервисы"])
        checks.append(("грязный черновик не проходит", code == 1))
        with open(dirty, encoding="utf-8") as handle:
            body = handle.read()
        checks.append(("тире в файле не осталось", "\u2014" not in body))
        checks.append(("футер дописан", "ЧАТ" in body))

        # Шаг статьи обязан ловить чужую подпись: дефект из отзыва автора.
        bad_article = os.path.join(tmp, "article-bad.md")
        with open(bad_article, "w", encoding="utf-8") as handle:
            handle.write("Семь сервисов для учёбы без карты и без VPN\n\n"
                         "Автор: badVIno\n\nГонял каждый неделю.\n")
        second = os.path.join(tmp, "good-2.txt")
        shutil.copyfile(os.path.join(HERE, "sample-post.txt"), second)
        code, out = quiet([second, "--article", bad_article])
        checks.append(("ship смотрит статью", "ШАГ 5. СТАТЬЯ" in out))
        checks.append(("чужая подпись в статье валит ship",
                       code == 1 and "E-A-AUTHOR" in out))
        clean_source = os.path.join(HERE, "tests", "article-good-01.md")
        if os.path.exists(clean_source):
            third = os.path.join(tmp, "good-3.txt")
            shutil.copyfile(os.path.join(HERE, "sample-post.txt"), third)
            code, out = quiet([third, "--article", clean_source])
            checks.append(("чистая статья не мешает ship",
                           code == 0 and "статья чистая" in out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    here = os.getcwd()
    away = None
    try:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            guard()
        checks.append(("гард молчит в каталоге скилла",
                       buf.getvalue() == "" or os.path.abspath(here) != SKILL))
        away = tempfile.mkdtemp(prefix="viora-away-")
        os.chdir(away)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            guard()
        checks.append(("гард подсказывает из чужого каталога", "cd " in buf.getvalue()))
        checks.append(("маршрут работает из чужого каталога",
                       route_text("нужен пак абузов")[1] == "абуз"))
    finally:
        os.chdir(here)
        if away:
            shutil.rmtree(away, ignore_errors=True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_code = hook(["Notion", "Business", "без", "карты", "--rubric", "абуз"])
    hook_out = buf.getvalue()
    checks.append(("крючок отдаёт код 0", hook_code == 0))
    checks.append(("крючок печатает все шесть архетипов",
                   all(("[%s]" % name) in hook_out for name in HOOK_ORDER)))
    checks.append(("крючок держит рубрику", "#абуз" in hook_out))
    checks.append(("архетипы расписаны на все рубрики",
                   sorted(HOOK_BY_RUBRIC) == sorted(ROUTES)))
    checks.append(("заготовка крючка влезает в первый экран",
                   all(len(template % "Notion Business без карты") <= 100
                       for _, template in HOOK_ARCHETYPES.values())))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        empty_code = hook([])
    checks.append(("крючок без темы просит тему", empty_code == 2))

    rows = doctor_rows(offline=True)
    names = [name for name, _, _, _ in rows]
    for wanted in ("питон", "файлы скилла", "скрипты", "память канала", "под-скиллы"):
        checks.append(("доктор смотрит %s" % wanted, wanted in names))
    checks.append(("доктор видит все скрипты",
                   all(ok for name, ok, _, _ in rows if name == "скрипты")))
    checks.append(("без сети доктор не ругается",
                   all(ok for name, ok, _, _ in rows if name == "сеть для ресерча")))
    checks.append(("доктор смотрит имя питона", "имя питона" in names))
    checks.append(("на линуксе имя питона python3",
                   python_call(which=lambda name: "/usr/bin/" + name
                               if name in ("python3", "python") else None)[0] == "python3"))
    checks.append(("на windows зовём python",
                   python_call(which=lambda name: "C:/Python/python.exe"
                               if name == "python" else None)[0] == "python"))
    checks.append(("остался только py, зовём py -3",
                   python_call(which=lambda name: "C:/Windows/py.exe"
                               if name == "py" else None)[0] == "py -3"))
    call_value, found_value, hint_value = python_hint(which=lambda name: None)
    checks.append(("без питона в PATH советуем полный путь",
                   not found_value and "полным путём" in hint_value))
    checks.append(("подсказка про питон непустая", bool(call_value) and bool(hint_value)))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_code_value = doctor(["--offline"])
    doctor_out = buf.getvalue()
    checks.append(("доктор отдаёт код 0", doctor_code_value == 0))
    checks.append(("доктор печатает имя питона", "имя питона" in doctor_out))
    checks.append(("доктор печатает вердикт",
                   "можно работать" in doctor_out or "ресерч и видео пока нет" in doctor_out))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor(["--offline", "--json"])
    try:
        import json as _json
        parsed = _json.loads(buf.getvalue())
        json_ok = isinstance(parsed.get("rows"), list) and len(parsed["rows"]) >= 8
    except ValueError:
        json_ok = False
    checks.append(("доктор отдаёт json", json_ok))

    failed = 0
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failed += 1
    print("Итого: %d проверок точки входа, провалилось %d" % (len(checks), failed))
    return 1 if failed else 0


def selftest_all():
    """Свои проверки плюс все чужие селфтесты одной командой."""
    fail = 0
    print("== точка входа ==")
    if own_selftest():
        fail = 1
    for name, script, flags in SELFTESTS:
        print("")
        print("== %s ==" % name)
        if run_script(script, flags):
            fail = 1
    print("")
    print("ВСЁ ЧИСТО" if not fail else "ЕСТЬ ПРОБЛЕМЫ, смотри выше")
    return fail


def usage():
    print("VIORA STUDIO, точка входа %s" % VERSION)
    print("")
    print("Главное, если читаешь это впервые:")
    print('  python3 tools/viora.py route "твоя задача словами"   план: рубрика, каркас, один файл на чтение')
    print("  python3 tools/viora.py ship draft.txt --rubric абуз   вся проверка за один запуск")
    print('  python3 tools/viora.py hook "тема" --rubric абуз      шесть вариантов первой строки')
    print("")
    print("Флаги ship: --rubric имя, --media если пост с фото, --min число порог оценки (по умолчанию %d), --article путь к статье" % MIN_DEFAULT)
    print("")
    print("Остальные команды пробрасывают аргументы в свой скрипт:")
    print("  fix draft.txt --rubric абуз     починить вёрстку и записать файл")
    print("  lint draft.txt --strict          только линтер поста")
    print("  score draft.txt                  оценка 0-100 на цифрах канала")
    print("  split draft.txt                  нужна ли статья на телеграф")
    print("  article article.md               линтер статьи для телеграфа")
    print("  chat answer.txt                  линтер речи агента")
    print("  memory show                      что уже выходило и какие цифры")
    print("  research запрос                  факты и источники из интернета")
    print("  video ссылка                      разбор видео с таймкодами")
    print("  tg next --wait 600 --json        заявки из Избранного в Телеграме")
    print("  life pc                          место на диске, память, батарея, адрес")
    print("  life weather Казань              погода на сегодня и завтра без ключей")
    print("  life morning                     утренний дайджест одним сообщением")
    print('  schedule add "08:00 morning"      напоминание в расписание')
    print("  schedule due --now 2026-09-06T08:00   что пора сделать в эту минуту")
    print("")
    print("Служебное:")
    print("  doctor         среда: питон и его имя, файлы, память, сеть, yt-dlp")
    print("  install --target /путь   поставить скилл в свой проект")
    print("  validate       проверить упаковку по спеке Agent Skills")
    print("  evals          прогнать 30 эталонов поведения")
    print("  package        собрать поставку в dist/")
    print("  check          все проверки скилла сразу (tools/check_all.sh)")
    print("  selftest       свои тесты плюс четырнадцать чужих наборов")
    print("  selftest-own   только тесты самой точки входа")
    print("  version        номер версии")


def guard():
    """Подсказать, откуда запускать, если запустили из чужого каталога.

    Сами пути внутри скилла считаются от этого файла, так что команда
    работает из любого места. Но в документации команды написаны от
    каталога скилла, и слабая модель теряется, когда видит tools/ и не
    находит его. Одна строка подсказки снимает весь класс ошибок.
    Переопределяется переменной окружения VIORA_SKILL_DIR.
    """
    override = os.environ.get("VIORA_SKILL_DIR")
    if override and os.path.isdir(override):
        return SKILL
    if os.path.abspath(os.getcwd()) != SKILL:
        sys.stderr.write(
            "Подсказка: команды скилла пишутся от каталога скилла. "
            "Сделай cd \"%s\" или запускай с полным путём.\n" % SKILL
        )
    return SKILL


def main(argv):
    if not argv:
        usage()
        return 2

    command = argv[0]
    rest = argv[1:]

    if command not in ("help", "-h", "--help", "version", "--version"):
        guard()

    if command in ("help", "-h", "--help"):
        usage()
        return 0
    if command in ("version", "--version"):
        print("viora.py %s" % VERSION)
        return 0
    if command == "route":
        brand, rest = take_brand(rest)
        return route(" ".join(rest), brand)
    if command == "hook":
        brand, rest = take_brand(rest)
        return hook(rest, brand)
    if command == "ship":
        brand, rest = take_brand(rest)
        return ship(rest, brand)
    if command == "fix":
        return run_script("lint_post.py", list(rest) + ["--fix"])
    if command == "doctor":
        return doctor(rest)
    if command == "check":
        return check()
    if command == "selftest":
        return selftest_all()
    if command == "selftest-own":
        return own_selftest()
    if command == "article":
        return article(rest)
    if command in PASS_THROUGH:
        return run_script(PASS_THROUGH[command], rest)

    print("Не знаю команду: %s" % command)
    print("")
    usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
