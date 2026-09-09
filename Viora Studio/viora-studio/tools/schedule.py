#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Расписание напоминаний: что и когда спросить у автора. Без вечного цикла.

Скрипт только считает и хранит. Он отвечает на один вопрос: какие задачи надо
выполнить в эту минуту. Кто спрашивает и кто отправляет сообщение, решает
сторож в Телеграме, его добавит следующий исполнитель.

Хранилище: memory/reminders.json. Это настройки автора, а не кэш, поэтому файл
едет вместе со скиллом, а не в рабочую папку моста.

Виды задач:
    morning   утренний дайджест: python3 tools/life.py morning
    weather   погода: python3 tools/life.py weather
    pc        состояние компа: python3 tools/life.py pc
    text      просто текст, который надо напомнить автору

Команды:
    python3 tools/schedule.py add "08:00 morning"
    python3 tools/schedule.py add "пн,ср 09:30 text: выпить таблетку"
    python3 tools/schedule.py list
    python3 tools/schedule.py remove 2
    python3 tools/schedule.py due --now "2026-09-06T08:00"
    python3 tools/schedule.py mark 2 --at "2026-09-06T09:30"
    python3 tools/schedule.py --selftest

Часовой пояс берётся из memory/profile.md, поле "часовой пояс". Нет пояса или
нет zoneinfo в питоне: считаем по локальному времени машины и пишем об этом
честно в поле tz.

На Windows вместо python3 пиши python или py -3.
"""

import argparse
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:  # noqa: BLE001
    ZoneInfo = None

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
STORE_VERSION = 1

KINDS = ("morning", "weather", "pc", "text")
KIND_WORDS = {
    "morning": ("morning", "утро", "утром", "дайджест"),
    "weather": ("weather", "погода", "погоду"),
    "pc": ("pc", "пк", "комп", "диск", "место"),
    "text": ("text", "текст", "напомни", "напоминание"),
}
KIND_TITLES = {
    "morning": "утренний дайджест",
    "weather": "погода",
    "pc": "состояние компа",
    "text": "напоминание",
}

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SHORT = {
    "mon": "пн",
    "tue": "вт",
    "wed": "ср",
    "thu": "чт",
    "fri": "пт",
    "sat": "сб",
    "sun": "вс",
}
DAY_WORDS = {
    "пн": "mon", "пон": "mon", "понедельник": "mon", "mon": "mon",
    "вт": "tue", "втор": "tue", "вторник": "tue", "tue": "tue",
    "ср": "wed", "среда": "wed", "среду": "wed", "wed": "wed",
    "чт": "thu", "четверг": "thu", "thu": "thu",
    "пт": "fri", "пятница": "fri", "пятницу": "fri", "fri": "fri",
    "сб": "sat", "суббота": "sat", "субботу": "sat", "sat": "sat",
    "вс": "sun", "воскресенье": "sun", "sun": "sun",
}
DAY_GROUPS = {
    "ежедневно": DAY_KEYS,
    "каждый": DAY_KEYS,
    "каждыйдень": DAY_KEYS,
    "всегда": DAY_KEYS,
    "daily": DAY_KEYS,
    "будни": ("mon", "tue", "wed", "thu", "fri"),
    "рабочие": ("mon", "tue", "wed", "thu", "fri"),
    "выходные": ("sat", "sun"),
}

# Слова-обращения, которые автор ставит перед временем в живой речи.
# «напомни 08:00 утро» и «каждый день в 08:00 утро» значат одно и то же.
FILLER_WORDS = ("напомни", "напоминай", "напоминание", "напомнить", "в", "во",
                "мне", "пожалуйста", "день", "каждое", "утро,", "расписание")

_NEIGHBOURS = {}


def neighbour(name):
    """Соседний скрипт из tools как модуль. Нет файла, значит None.

    Импортируем по пути, а не через sys.path: скилл лежит в папке с пробелом,
    а в трёх папках обнаружения теперь заглушки. Путь надёжнее.
    """
    if name in _NEIGHBOURS:
        return _NEIGHBOURS[name]
    path = os.path.join(HERE, name + ".py")
    module = None
    if os.path.exists(path):
        try:
            spec = importlib.util.spec_from_file_location("viora_schedule_" + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001
            module = None
    _NEIGHBOURS[name] = module
    return module


def profile_value(key, root=None):
    """Поле профиля автора или пустая строка. Профиль имеет право быть пустым."""
    module = neighbour("memory")
    if module is None or not hasattr(module, "profile_get"):
        return ""
    try:
        return module.profile_get(root or SKILL, key) or ""
    except Exception:  # noqa: BLE001
        return ""


def store_path(root=None):
    """Путь к хранилищу. Настройки автора живут в памяти скилла."""
    return os.path.join(root or SKILL, "memory", "reminders.json")


def empty_store():
    return {"version": STORE_VERSION, "reminders": []}


def read_store(root=None, path=None):
    """Чтение хранилища. Битый или чужой файл не роняет скрипт."""
    full = path or store_path(root)
    if not os.path.exists(full):
        return empty_store()
    try:
        with io.open(full, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:  # noqa: BLE001
        return empty_store()
    if not isinstance(data, dict):
        return empty_store()
    rows = data.get("reminders")
    if not isinstance(rows, list):
        rows = []
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {
            "id": int(row.get("id") or 0),
            "time": str(row.get("time") or ""),
            "days": [d for d in (row.get("days") or []) if d in DAY_KEYS],
            "kind": str(row.get("kind") or "text"),
            "text": str(row.get("text") or ""),
            "created": str(row.get("created") or ""),
            "last_done": str(row.get("last_done") or ""),
        }
        if item["id"] and item["time"] and item["kind"] in KINDS:
            clean.append(item)
    return {"version": int(data.get("version") or STORE_VERSION), "reminders": clean}


def write_store(data, root=None, path=None):
    """Запись хранилища. Держим текст читаемым: файл правят руками."""
    full = path or store_path(root)
    folder = os.path.dirname(full)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    body = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    with io.open(full, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body + "\n")
    return full


def next_id(rows):
    """Номера не переиспользуем: автор мог записать номер в свой список дел."""
    return max([int(row.get("id") or 0) for row in rows] or [0]) + 1


def parse_time(token):
    """08:00 и 8:00 понимаем, 25:00 и 08:70 нет."""
    token = (token or "").strip().replace(".", ":")
    if ":" not in token:
        return ""
    head, _, tail = token.partition(":")
    if not head.isdigit() or not tail.isdigit() or len(tail) != 2:
        return ""
    hour, minute = int(head), int(tail)
    if hour > 23 or minute > 59:
        return ""
    return "%02d:%02d" % (hour, minute)


def parse_days(token):
    """пн,ср и будни превращаем в ключи дней. Не дни, значит пустой список."""
    token = (token or "").strip().lower().replace(" ", "")
    if not token:
        return []
    if token in DAY_GROUPS:
        return list(DAY_GROUPS[token])
    days = []
    for part in token.replace(";", ",").split(","):
        part = part.strip(".")
        if not part:
            continue
        if "-" in part:
            left, _, right = part.partition("-")
            if left in DAY_WORDS and right in DAY_WORDS:
                start = DAY_KEYS.index(DAY_WORDS[left])
                stop = DAY_KEYS.index(DAY_WORDS[right])
                span = list(range(start, stop + 1)) if start <= stop else []
                for index in span:
                    days.append(DAY_KEYS[index])
                continue
            return []
        if part not in DAY_WORDS:
            return []
        days.append(DAY_WORDS[part])
    seen, out = set(), []
    for day in days:
        if day not in seen:
            seen.add(day)
            out.append(day)
    return out


def parse_kind(token):
    """Слово вида задачи в один из четырёх видов."""
    token = (token or "").strip().lower().rstrip(":")
    for kind, words in KIND_WORDS.items():
        if token == kind or token in words:
            return kind
    return ""


def parse_add(line):
    """Разбор строки заявки. Возвращает (запись, беда).

    Формат: [дни] ЧЧ:ММ вид[: текст]. Дни необязательны, по умолчанию каждый день.
    Из Телеграма заявка приходит живой речью: «напомни 09:30 выпить таблетку».
    Поэтому слова-обращения впереди отбрасываются, а незнакомое слово после
    времени считается началом текста напоминания, а не ошибкой.
    """
    line = " ".join((line or "").split())
    if not line:
        return None, "пустая строка, пример: 08:00 morning"
    head, _, tail = line.partition(":")
    if head and tail and parse_kind(head) and parse_time(head.split(" ")[-1]) == "":
        return None, "сначала время, потом вид: 09:30 text: выпить таблетку"
    words = line.split(" ")
    while words and words[0].lower().strip(",") in FILLER_WORDS:
        words = words[1:]
    if not words:
        return None, "не понял время, пиши 08:00"
    days = []
    if parse_time(words[0]) == "" and len(words) > 1:
        days = parse_days(words[0])
        if not days:
            return None, "не понял дни: %s, пиши пн,ср или будни" % words[0]
        words = words[1:]
        while words and words[0].lower().strip(",") in FILLER_WORDS:
            words = words[1:]
    moment = parse_time(words[0]) if words else ""
    if not moment:
        return None, "не понял время, пиши 08:00"
    words = words[1:]
    if not words:
        return None, "не понял вид задачи, пиши morning, weather, pc или text"
    kind = parse_kind(words[0])
    if kind:
        rest = " ".join(words[1:]).strip()
    else:
        # Живая речь: «09:30 выпить таблетку» это text с таким текстом.
        kind = "text"
        rest = " ".join(words).strip()
    if rest.startswith(":"):
        rest = rest[1:].strip()
    if kind == "text" and not rest:
        return None, "вид text без текста, пиши: 09:30 text: выпить таблетку"
    if kind != "text" and rest:
        return None, "вид %s не берёт текст, для своих слов есть text" % kind
    return {
        "id": 0,
        "time": moment,
        "days": days or list(DAY_KEYS),
        "kind": kind,
        "text": rest,
        "created": "",
        "last_done": "",
    }, ""


def days_text(days):
    """Дни для человека: каждый день, будни, выходные или список."""
    keys = [day for day in DAY_KEYS if day in (days or [])]
    if len(keys) == 7:
        return "каждый день"
    if keys == list(DAY_GROUPS["будни"]):
        return "будни"
    if keys == list(DAY_GROUPS["выходные"]):
        return "выходные"
    return ",".join(DAY_SHORT[day] for day in keys) or "каждый день"


def row_title(item):
    """Что именно случится: свой текст важнее названия вида."""
    if item.get("kind") == "text":
        return item.get("text") or KIND_TITLES["text"]
    return KIND_TITLES.get(item.get("kind"), item.get("kind") or "")


def tz_name(root=None):
    """Имя пояса из профиля автора. Пусто, значит время машины."""
    return (profile_value("часовой пояс", root=root) or "").strip()


def zone_of(name):
    """Пояс по имени. Нет zoneinfo или имя чужое, значит None."""
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None


def parse_moment(text, zone=None):
    """2026-09-06T08:00 или 2026-09-06 08:00 в datetime нужного пояса."""
    raw = (text or "").strip().replace(" ", "T")
    if not raw:
        return None
    if len(raw) == 16:
        raw = raw + ":00"
    try:
        moment = datetime.fromisoformat(raw)
    except Exception:  # noqa: BLE001
        return None
    if zone is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment


def now_moment(text="", root=None, zone=None):
    """Момент расчёта: из ключа --now или текущее время в поясе автора."""
    if zone is None:
        zone = zone_of(tz_name(root))
    if text:
        return parse_moment(text, zone=zone), zone
    if zone is not None:
        return datetime.now(tz=zone), zone
    return datetime.now(), zone


def stamp(moment):
    """Минута в строку: секунды и пояс в хранилище не нужны."""
    return moment.strftime("%Y-%m-%dT%H:%M") if moment else ""


def day_key(moment):
    return DAY_KEYS[moment.weekday()]


def hits_minute(item, moment):
    """Пора ли: совпал день недели и совпала минута."""
    if day_key(moment) not in (item.get("days") or []):
        return False
    return item.get("time") == moment.strftime("%H:%M")


def done_today(item, moment):
    """Уже сделано сегодня: сравниваем календарный день отметки."""
    mark = (item.get("last_done") or "").strip()
    if not mark:
        return False
    return mark[:10] == moment.strftime("%Y-%m-%d")


def due_items(data, moment):
    """Задачи на эту минуту, которых сегодня ещё не делали."""
    out = []
    for item in data.get("reminders") or []:
        if hits_minute(item, moment) and not done_today(item, moment):
            out.append(item)
    return sorted(out, key=lambda row: (row.get("time"), row.get("id")))


def command_of(item):
    """Чем задачу выполнить. Для text команды нет, это просто слова автору."""
    if item.get("kind") == "text":
        return ""
    return "python3 tools/life.py %s" % item.get("kind")


def today_lines(now=None, root=None):
    """Строки напоминаний на сегодня для дайджеста life.py morning."""
    zone = zone_of(tz_name(root))
    if isinstance(now, datetime):
        moment = now
    elif isinstance(now, str) and now:
        moment = parse_moment(now, zone=zone)
    else:
        moment = datetime.now(tz=zone) if zone is not None else datetime.now()
    if moment is None:
        return []
    key = day_key(moment)
    rows = [item for item in read_store(root=root).get("reminders") or []
            if key in (item.get("days") or [])]
    rows.sort(key=lambda row: (row.get("time"), row.get("id")))
    return ["%s %s" % (row.get("time"), row_title(row)) for row in rows]


def cmd_add(args, root):
    """Новая запись в расписание."""
    line = " ".join(args.rest).strip()
    item, trouble = parse_add(line)
    if trouble:
        print("Не понял заявку: %s" % trouble)
        print('Примеры: "08:00 morning" или "пн,ср 09:30 text: выпить таблетку"')
        return 1
    data = read_store(root=root)
    moment, _zone = now_moment(args.at, root=root)
    item["id"] = next_id(data.get("reminders") or [])
    item["created"] = stamp(moment) if moment else ""
    data.setdefault("reminders", []).append(item)
    write_store(data, root=root)
    if args.as_json:
        print(json.dumps(item, ensure_ascii=False))
        return 0
    print("Записал %d: %s %s %s"
          % (item["id"], days_text(item["days"]), item["time"], row_title(item)))
    return 0


def cmd_list(args, root):
    """Всё расписание одним списком."""
    data = read_store(root=root)
    rows = sorted(data.get("reminders") or [],
                  key=lambda row: (row.get("time"), row.get("id")))
    if args.as_json:
        print(json.dumps({"count": len(rows), "reminders": rows},
                         ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("Напоминаний нет.")
        print('Пример: python3 tools/schedule.py add "08:00 morning"')
        return 0
    print("Напоминаний: %d" % len(rows))
    for row in rows:
        mark = row.get("last_done") or "не было"
        print("  %s. %s %s %s, последний раз: %s"
              % (row.get("id"), days_text(row.get("days")), row.get("time"),
                 row_title(row), mark))
    return 0


def cmd_remove(args, root):
    """Убрать запись по номеру."""
    if not args.rest:
        print("Скажи номер: python3 tools/schedule.py remove 2")
        return 2
    raw = args.rest[0]
    if not raw.isdigit():
        print("Номер это число, а не %s" % raw)
        return 2
    wanted = int(raw)
    data = read_store(root=root)
    rows = data.get("reminders") or []
    gone = [row for row in rows if int(row.get("id")) == wanted]
    if not gone:
        print("Нет напоминания с номером %d" % wanted)
        return 1
    data["reminders"] = [row for row in rows if int(row.get("id")) != wanted]
    write_store(data, root=root)
    print("Убрал %d: %s %s" % (wanted, gone[0].get("time"), row_title(gone[0])))
    return 0


def cmd_due(args, root):
    """Задачи на эту минуту в JSON. Эту команду будет звать сторож."""
    zone = zone_of(tz_name(root))
    moment, zone = now_moment(args.now, root=root, zone=zone)
    if moment is None:
        print(json.dumps({"error": "не понял время, пиши 2026-09-06T08:00"},
                         ensure_ascii=False))
        return 1
    rows = due_items(read_store(root=root), moment)
    payload = {
        "now": stamp(moment),
        "tz": tz_name(root) or "время машины",
        "count": len(rows),
        "due": [{
            "id": row.get("id"),
            "time": row.get("time"),
            "kind": row.get("kind"),
            "text": row.get("text"),
            "title": row_title(row),
            "command": command_of(row),
            "days": row.get("days"),
        } for row in rows],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_mark(args, root):
    """Отметка выполнения, чтобы задача не повторилась сегодня."""
    if not args.rest:
        print("Скажи номер: python3 tools/schedule.py mark 2 --at 2026-09-06T08:00")
        return 2
    raw = args.rest[0]
    if not raw.isdigit():
        print("Номер это число, а не %s" % raw)
        return 2
    wanted = int(raw)
    zone = zone_of(tz_name(root))
    moment, zone = now_moment(args.at or args.now, root=root, zone=zone)
    if moment is None:
        print("Не понял время, пиши 2026-09-06T08:00")
        return 1
    data = read_store(root=root)
    found = None
    for row in data.get("reminders") or []:
        if int(row.get("id")) == wanted:
            row["last_done"] = stamp(moment)
            found = row
            break
    if found is None:
        print("Нет напоминания с номером %d" % wanted)
        return 1
    write_store(data, root=root)
    if args.as_json:
        print(json.dumps(found, ensure_ascii=False))
        return 0
    print("Отметил %d: %s, %s" % (wanted, row_title(found), found["last_done"]))
    return 0


class FakeMemory(object):
    """Подмена памяти для тестов: живой профиль автора трогать нельзя."""

    @staticmethod
    def profile_get(root, key):
        return "Europe/Astrakhan" if key == "часовой пояс" else ""


def selftest():
    """Тесты без сети, без часов и без живого хранилища."""
    import shutil
    import tempfile

    checks = []
    _NEIGHBOURS["memory"] = None

    checks.append(("время 08:00 понятно", parse_time("08:00") == "08:00"))
    checks.append(("время 8:00 дополняется нулём", parse_time("8:00") == "08:00"))
    checks.append(("точка вместо двоеточия тоже время", parse_time("7.05") == "07:05"))
    checks.append(("25 часов не время", parse_time("25:00") == ""))
    checks.append(("70 минут не время", parse_time("08:70") == ""))
    checks.append(("слово не время", parse_time("утро") == ""))

    checks.append(("пн,ср в ключи дней", parse_days("пн,ср") == ["mon", "wed"]))
    checks.append(("будни это пять дней", len(parse_days("будни")) == 5))
    checks.append(("выходные это сб и вс", parse_days("выходные") == ["sat", "sun"]))
    checks.append(("ежедневно это семь дней", len(parse_days("ежедневно")) == 7))
    checks.append(("диапазон пн-пт работает", parse_days("пн-пт") == list(DAY_GROUPS["будни"])))
    checks.append(("мусор в днях не проходит", parse_days("виоралогия") == []))
    checks.append(("один день не дублируется", parse_days("пн,пн") == ["mon"]))

    checks.append(("morning узнаётся", parse_kind("morning") == "morning"))
    checks.append(("утро это morning", parse_kind("утро") == "morning"))
    checks.append(("погода это weather", parse_kind("погода") == "weather"))
    checks.append(("пк это pc", parse_kind("пк") == "pc"))
    checks.append(("текст это text", parse_kind("text:") == "text"))
    checks.append(("чужое слово не вид", parse_kind("пост") == ""))

    item, trouble = parse_add("08:00 morning")
    checks.append(("заявка 08:00 morning разобралась", not trouble and item is not None))
    checks.append(("без дней значит каждый день",
                   bool(item) and len(item["days"]) == 7 and item["kind"] == "morning"))
    item, trouble = parse_add("пн,ср 09:30 text: выпить таблетку")
    checks.append(("заявка с днями и текстом разобралась", not trouble and item is not None))
    checks.append(("дни из заявки сохранились",
                   bool(item) and item["days"] == ["mon", "wed"] and item["time"] == "09:30"))
    checks.append(("текст напоминания сохранился",
                   bool(item) and item["text"] == "выпить таблетку" and item["kind"] == "text"))
    checks.append(("text без текста не проходит", parse_add("09:30 text")[1] != ""))
    checks.append(("weather с текстом не проходит", parse_add("08:00 weather: Казань")[1] != ""))
    checks.append(("пустая заявка не проходит", parse_add("")[1] != ""))
    checks.append(("вид без времени не проходит", parse_add("morning")[1] != ""))
    checks.append(("сначала время, потом вид", parse_add("text: выпить таблетку")[1] != ""))

    # Живая речь из Телеграма: обращение впереди и текст без слова text
    item, trouble = parse_add("напомни 09:30 выпить таблетку")
    checks.append(("напомни ЧЧ:ММ слова это text",
                   not trouble and item["kind"] == "text" and item["text"] == "выпить таблетку"))
    item, trouble = parse_add("напоминай 08:00 утро")
    checks.append(("напоминай 08:00 утро это morning", not trouble and item["kind"] == "morning"))
    item, trouble = parse_add("каждый день в 07:30 погода")
    checks.append(("каждый день в ЧЧ:ММ погода",
                   not trouble and item["kind"] == "weather" and len(item["days"]) == 7))
    item, trouble = parse_add("будни 09:00 созвон с командой")
    checks.append(("дни плюс живой текст",
                   not trouble and item["days"] == list(DAY_GROUPS["будни"])
                   and item["text"] == "созвон с командой"))
    checks.append(("одно слово-обращение без времени не проходит", parse_add("напомни")[1] != ""))

    checks.append(("дни для человека: каждый день", days_text(list(DAY_KEYS)) == "каждый день"))
    checks.append(("дни для человека: будни",
                   days_text(list(DAY_GROUPS["будни"])) == "будни"))
    checks.append(("дни для человека: список", days_text(["mon", "wed"]) == "пн,ср"))
    checks.append(("заголовок вида есть",
                   row_title({"kind": "morning"}) == "утренний дайджест"))
    checks.append(("свой текст важнее названия вида",
                   row_title({"kind": "text", "text": "полить цветы"}) == "полить цветы"))
    checks.append(("команда для вида есть",
                   command_of({"kind": "pc"}) == "python3 tools/life.py pc"))
    checks.append(("у text команды нет", command_of({"kind": "text"}) == ""))

    checks.append(("номер начинается с единицы", next_id([]) == 1))
    checks.append(("номера не переиспользуются",
                   next_id([{"id": 1}, {"id": 7}]) == 8))

    moment = parse_moment("2026-09-07T09:30")
    checks.append(("минута с тире и T разобралась", moment is not None))
    checks.append(("пробел вместо T тоже работает",
                   parse_moment("2026-09-07 09:30") == moment))
    checks.append(("мусор во времени даёт None", parse_moment("вчера") is None))
    checks.append(("метка времени без секунд", stamp(moment) == "2026-09-07T09:30"))
    checks.append(("понедельник это mon", day_key(moment) == "mon"))

    pill = {"id": 1, "time": "09:30", "days": ["mon", "wed"], "kind": "text",
            "text": "выпить таблетку", "created": "", "last_done": ""}
    digest = {"id": 2, "time": "08:00", "days": list(DAY_KEYS), "kind": "morning",
              "text": "", "created": "", "last_done": ""}
    checks.append(("в свою минуту задача срабатывает", hits_minute(pill, moment)))
    checks.append(("в другой день не срабатывает",
                   not hits_minute(pill, parse_moment("2026-09-08T09:30"))))
    checks.append(("в другую минуту не срабатывает",
                   not hits_minute(pill, parse_moment("2026-09-07T09:31"))))
    checks.append(("отметка за сегодня видна",
                   done_today(dict(pill, last_done="2026-09-07T09:30"), moment)))
    checks.append(("отметка за вчера не мешает",
                   not done_today(dict(pill, last_done="2026-09-06T09:30"), moment)))

    data = {"version": STORE_VERSION, "reminders": [pill, digest]}
    checks.append(("в минуту таблетки должна одна задача",
                   [row["id"] for row in due_items(data, moment)] == [1]))
    checks.append(("выполненная задача больше не должна",
                   due_items({"reminders": [dict(pill, last_done="2026-09-07T09:30")]},
                             moment) == []))
    checks.append(("в чужую минуту задач нет",
                   due_items(data, parse_moment("2026-09-07T11:11")) == []))

    checks.append(("пояс без имени это None", zone_of("") is None))
    checks.append(("выдуманный пояс это None", zone_of("Мухосранск/Центр") is None))
    checks.append(("живой пояс берётся, если есть zoneinfo",
                   ZoneInfo is None or zone_of("Europe/Astrakhan") is not None))
    checks.append(("без памяти пояс пуст", tz_name() == ""))
    _NEIGHBOURS["memory"] = FakeMemory
    checks.append(("пояс берётся из профиля", tz_name() == "Europe/Astrakhan"))
    _NEIGHBOURS["memory"] = None

    folder = tempfile.mkdtemp(prefix="viora-schedule-")
    try:
        checks.append(("пустое хранилище не роняет", read_store(root=folder)["reminders"] == []))
        write_store({"version": STORE_VERSION, "reminders": [pill]}, root=folder)
        checks.append(("запись читается обратно",
                       read_store(root=folder)["reminders"][0]["text"] == "выпить таблетку"))
        checks.append(("хранилище лежит в памяти скилла",
                       store_path(folder).endswith(os.path.join("memory", "reminders.json"))))
        with io.open(store_path(folder), "w", encoding="utf-8") as handle:
            handle.write("это не json")
        checks.append(("битый файл не роняет", read_store(root=folder)["reminders"] == []))
        with io.open(store_path(folder), "w", encoding="utf-8") as handle:
            handle.write('[1, 2, 3]')
        checks.append(("чужая структура не роняет", read_store(root=folder)["reminders"] == []))
        with io.open(store_path(folder), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"reminders": [{"id": 5, "time": "08:00", "kind": "чужое"}]}))
        checks.append(("чужой вид задачи отбрасывается", read_store(root=folder)["reminders"] == []))
        os.remove(store_path(folder))

        code = main(["add", "08:00 morning", "--root", folder])
        checks.append(("cli записывает дайджест", code == 0))
        code = main(["add", "пн,ср 09:30 text: выпить таблетку", "--root", folder])
        checks.append(("cli записывает таблетку", code == 0))
        rows = read_store(root=folder)["reminders"]
        checks.append(("в хранилище две записи", len(rows) == 2))
        checks.append(("номера разные", rows[0]["id"] != rows[1]["id"]))
        checks.append(("cli ругается на мусор",
                       main(["add", "когда-нибудь потом", "--root", folder]) == 1))
        checks.append(("cli показывает список", main(["list", "--root", folder]) == 0))
        checks.append(("cli отдаёт список в json",
                       main(["list", "--root", folder, "--json"]) == 0))
        checks.append(("cli без номера просит номер",
                       main(["remove", "--root", folder]) == 2))
        checks.append(("cli не видит чужой номер",
                       main(["remove", "99", "--root", folder]) == 1))
        checks.append(("cli требует число",
                       main(["remove", "два", "--root", folder]) == 2))

        buf = io.StringIO()
        saved = sys.stdout
        sys.stdout = buf
        try:
            due_code = main(["due", "--now", "2026-09-07T09:30", "--root", folder])
        finally:
            sys.stdout = saved
        payload = json.loads(buf.getvalue())
        checks.append(("due отдаёт код 0", due_code == 0))
        checks.append(("due отдаёт json с задачами", payload["count"] == 1))
        checks.append(("due видит именно таблетку",
                       payload["due"][0]["text"] == "выпить таблетку"))
        checks.append(("due говорит про пояс", bool(payload["tz"])))
        due_id = payload["due"][0]["id"]

        buf = io.StringIO()
        sys.stdout = buf
        try:
            main(["due", "--now", "2026-09-07T08:00", "--root", folder])
        finally:
            sys.stdout = saved
        payload = json.loads(buf.getvalue())
        checks.append(("в восемь утра ждёт дайджест",
                       payload["count"] == 1 and payload["due"][0]["kind"] == "morning"))
        checks.append(("у дайджеста есть команда",
                       payload["due"][0]["command"].endswith("life.py morning")))

        checks.append(("mark ставит отметку",
                       main(["mark", str(due_id), "--at", "2026-09-07T09:30",
                             "--root", folder]) == 0))
        buf = io.StringIO()
        sys.stdout = buf
        try:
            main(["due", "--now", "2026-09-07T09:30", "--root", folder])
        finally:
            sys.stdout = saved
        payload = json.loads(buf.getvalue())
        checks.append(("после отметки задача молчит", payload["count"] == 0))
        checks.append(("mark не видит чужой номер",
                       main(["mark", "99", "--at", "2026-09-07T09:30", "--root", folder]) == 1))
        checks.append(("due ругается на мусор во времени",
                       main(["due", "--now", "вчера", "--root", folder]) == 1))

        lines = today_lines(now="2026-09-07T07:00", root=folder)
        checks.append(("строки на сегодня для дайджеста", len(lines) == 2))
        checks.append(("строка начинается со времени", lines[0].startswith("08:00")))
        checks.append(("в строке есть текст автора",
                       any("выпить таблетку" in line for line in lines)))
        checks.append(("в вторник таблетки нет",
                       len(today_lines(now="2026-09-08T07:00", root=folder)) == 1))
        checks.append(("мусор во времени даёт пустой список",
                       today_lines(now="никогда", root=folder) == []))
        checks.append(("без хранилища строк нет",
                       today_lines(now="2026-09-07T07:00",
                                   root=os.path.join(folder, "нету")) == []))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        _NEIGHBOURS.pop("memory", None)

    print("--- schedule selftest ---")
    bad = 0
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            bad += 1
    print("Итого: %d проверок расписания, провалилось %d" % (len(checks), bad))
    return 1 if bad else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="schedule.py",
        description="Расписание напоминаний без вечного цикла.",
        epilog='Пример: python3 tools/schedule.py add "пн,ср 09:30 text: выпить таблетку"',
    )
    parser.add_argument("command", nargs="?",
                        choices=("add", "list", "remove", "due", "mark"),
                        help="add, list, remove, due, mark")
    parser.add_argument("rest", nargs="*", help="строка заявки или номер записи")
    parser.add_argument("--now", default="", help="минута расчёта: 2026-09-06T08:00")
    parser.add_argument("--at", default="", help="минута отметки: 2026-09-06T08:00")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="ответ машине, а не человеку")
    parser.add_argument("--root", default="", help="корень скилла, по умолчанию свой")
    parser.add_argument("--selftest", action="store_true", help="свои тесты")
    parser.add_argument("--version", action="store_true", help="версия скрипта")
    args = parser.parse_args(argv)

    if args.version:
        print("schedule.py %s" % VERSION)
        return 0
    if args.selftest:
        return selftest()
    if not args.command:
        parser.print_help()
        return 2

    root = args.root or SKILL
    if args.command == "add":
        return cmd_add(args, root)
    if args.command == "list":
        return cmd_list(args, root)
    if args.command == "remove":
        return cmd_remove(args, root)
    if args.command == "due":
        return cmd_due(args, root)
    return cmd_mark(args, root)


if __name__ == "__main__":
    sys.exit(main())
