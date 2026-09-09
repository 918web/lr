#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg.py 1.0.0

Мост между Избранным в Телеграме и скиллом VIORA STUDIO.

Зачем он нужен. Автор пишет заявку в Избранное одной строкой, агент должен
понять её так же, как понял бы в прошлый раз, ничего не потерять при перезапуске
и не публиковать лишнего. Держать это в голове модели нельзя, поэтому состояние,
разбор команд и все тексты для автора живут здесь, в коде.

В Телеграм скрипт ходит сам, через telethon. Сервер MCP больше не нужен:
он был лишним звеном, и любая его ошибка выглядела как тишина в Избранном.
Ключи и строка сессии берутся из secrets/, поэтому команды работают из любой
оболочки, без экспорта переменных. Входящие заявки приносит сторож
tools/tg_watch.py или команда poll.

Команды:
    python3 tools/tg.py setup
    python3 tools/tg.py login --force
    python3 tools/tg.py login --phone
    python3 tools/tg.py doctor
    python3 tools/tg.py card
    python3 tools/tg.py poll --limit 30
    python3 tools/tg.py send --text "VIORA НА СМЕНЕ" --to me
    python3 tools/tg.py ping
    python3 tools/tg.py publish r1 --at 20:30
    python3 tools/tg.py next --wait 600 --json
    python3 tools/tg.py feed --stdin
    python3 tools/tg.py stage r1 --file out/r1.txt --rubric абуз
    python3 tools/tg.py sent r1 --msg-id 4821
    python3 tools/tg.py payload r1
    python3 tools/tg.py published r1 --msg-id 612 --link https://t.me/VioraStudio/612
    python3 tools/tg.py ask r1 --text "какой личный факт добавить"
    python3 tools/tg.py cancel r1
    python3 tools/tg.py status
    python3 tools/tg.py when --at 20:30
    python3 tools/tg.py config --set channel=@VioraStudio
    python3 tools/tg.py selftest

Рабочая папка моста: ~/.local/state/viora-studio, меняется через VIORA_TG_HOME.
Одна папка на все копии скилла: сторож и агент видят один и тот же ящик.

Один процесс в сети. Соединение с Телеграмом держит только сторож
tools/tg_watch.py. Пока он жив (lock-файл с сердцебиением в рабочей папке),
send, publish, stage --send, ask --send и ping не подключаются сами: они кладут
строку в tg-outbox.jsonl и ждут подтверждения с msg_id из tg-outbox-done.jsonl.
Сторожа нет: команда подключается сама, как раньше.

Коды возврата: 0 порядок, 1 проблема, 2 ошибка вызова.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time

VERSION = "1.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)

STATE_VERSION = 1
INBOX_KEEP = 400
INBOX_MAX = 800
MAX_WAIT = 900
MAX_TRIES = 3
BRIEF = 70
TG_LIMIT = 4096
DEVICE = "VIORA"

# Один процесс в сети: сторож жив, если его сердцебиение моложе этого срока.
HEARTBEAT_TTL = 20
# Сколько ждать подтверждения от сторожа, прежде чем считать отправку пропавшей.
OUTBOX_WAIT = 45
OUTBOX_KEEP = 300
# Строка сессии из окружения важнее файла, а файл важнее всего остального.
SESSION_ENVS = ("TELEGRAM_SESSION_STRING_WATCH", "TELEGRAM_WATCH_SESSION_STRING",
                "TELEGRAM_SESSION_STRING")
SESSION_KEY = "TELEGRAM_SESSION_STRING_WATCH"
SECRET_FILES = ("watch.env", "telegram.env")

DEFAULT_CONFIG = {
    "prefixes": ["/viora", "/виора", "/vs", "виора", "viora"],
    "marker": "VIORA",
    "inbox": "me",
    "channel": "@VioraStudio",
    "mode": "draft",
    "window": "16:30-21:00",
    "tz": "Europe/Astrakhan",
    "reply_capture": True,
    "rubric_default": "",
    "preview": False,
    # trust=edit разрешает агенту правку файлов по явной просьбе автора.
    # По умолчанию агент работает только на чтение: reference/telegram.md.
    "trust": "read",
}

MODES = ("draft", "scheduled", "auto")
TRUST_LEVELS = ("read", "edit")

# Слова, которые работают только без хвоста. "/viora что там по Notion" это
# заявка на пост, а не запрос статуса, иначе автор будет ловить сюрпризы.
SOLO = {
    "publish": ("пуб", "публикуй", "опубликуй", "публикую", "го", "ок", "окей", "go", "ship"),
    "cancel": ("стоп", "отмена", "отмени", "забудь", "хватит"),
    "status": ("что", "статус", "дела", "где"),
    "help": ("помощь", "help", "справка", "команды", "?"),
    "log": ("лог", "история"),
}

# Проверка связи. Автор пишет "/viora как дела" и ждёт ответ сразу, а не
# пост про то, как дела. Отвечает сам сторож, агента не будим.
PING = (
    "как дела", "как ты", "ты тут", "ты здесь", "жив", "живой", "тест",
    "проверка", "пинг", "ping", "test", "алло", "ау", "привет", "здарова",
    "ку", "на месте", "работаешь", "слышишь", "виора", "viora",
)

# Слова с хвостом: хвост это и есть смысл команды.
WITH_TAIL = {
    "edit": ("правь", "править", "правка", "переделай", "перепиши", "фикс"),
    "schedule": ("отложи", "отложить", "вечером"),
}

# Бытовые команды. Автор пишет "виора реши" и ждёт ответ, а не пост.
# Фразы из двух слов тоже годятся: match_command режет их по разделителю.
SOLVE = ("реши", "решить", "решай", "ответь", "объясни", "объяснить",
         "помоги", "что тут", "что здесь", "что это")
REMIND = ("напомни", "напоминай", "напоминание", "каждое утро", "расписание")
WEATHER = ("погода", "погоду", "погодка")
PC = ("пк", "комп", "компьютер", "место", "диск", "батарея",
      "статус компа", "что с компом")
MORNING = ("утро", "утром", "дайджест", "доброе утро")

HINTS = {
    "request": "Новая заявка. Разбери сырьё, недостающее спроси ОДНОЙ строкой через ask, "
               "собери пост, прогони ship, отдай через stage.",
    "edit": "Автор просит правку черновика. Правь только то, что просят, снова ship, снова stage.",
    "publish": "Автор дал добро. Возьми текст через payload и отправь в канал, потом published.",
    "schedule": "Автор просит отложенную публикацию. Время возьми через when, потом published.",
    "cancel": "Автор отменил заявку. Отметь cancel и ответь одной строкой.",
    "status": "Автор спросил статус. Отправь в Избранное вывод команды status.",
    "help": "Автор просит карточку команд. Отправь в Избранное вывод команды card.",
    "log": "Автор просит последние заявки. Отправь в Избранное вывод команды status.",
    "answer": "Это ответ на твой вопрос. Допиши пост с новым фактом и снова stage.",
    "resume": "Незакрытая заявка с прошлого запуска. Доведи её до конца или отмени.",
    "empty": "Ящик пуст. Ничего не делаем и не пишем автору.",
    "unknown": "Команда не разобрана. Ответь автору карточкой card.",
    "ping": "Проверка связи. Одна строка через tg.py ping, пост писать не надо.",
    "solve": "Автор просит решить или объяснить. Ответь по делу через tg.py send, "
             "пост писать не надо. Если в заявке есть media, сначала смотри файлы.",
    "pc": "Автор спросил про комп. Отправь вывод python3 tools/life.py pc.",
    "weather": "Автор спросил погоду. Отправь вывод python3 tools/life.py weather, "
                 "город без аргумента берётся из профиля.",
    "remind": "Автор просит напоминание. Запиши его через python3 tools/schedule.py add "
                "и подтверди одной строкой.",
    "morning": "Автор просит дайджест. Отправь вывод python3 tools/life.py morning.",
}


# ---------------------------------------------------------------- пути и файлы

def home():
    """Рабочая папка моста. Одна на машину, а не на копию скилла."""
    env = os.environ.get("VIORA_TG_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "viora-studio")


def paths():
    root = home()
    return {
        "home": root,
        "config": os.path.join(root, "tg-config.json"),
        "state": os.path.join(root, "tg-state.json"),
        "inbox": os.path.join(root, "tg-inbox.jsonl"),
        "outbox": os.path.join(root, "tg-outbox.jsonl"),
        "outbox_done": os.path.join(root, "tg-outbox-done.jsonl"),
        "lock": os.path.join(root, "tg-watch.lock"),
        "log": os.path.join(root, "watch.log"),
        "media": os.path.join(root, "media"),
        "drafts": os.path.join(root, "drafts"),
    }


def ensure_home():
    p = paths()
    os.makedirs(p["drafts"], exist_ok=True)
    os.makedirs(p["media"], exist_ok=True)
    try:
        os.chmod(p["home"], 0o700)
    except OSError:
        pass
    return p


def write_atomic(path, text, mode=0o600):
    """Запись без полуфабрикатов: сторож и агент читают файл в любой момент."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    got = read_json(paths()["config"], {})
    if isinstance(got, dict):
        for key, value in got.items():
            if key in cfg:
                cfg[key] = value
    if isinstance(cfg["prefixes"], str):
        cfg["prefixes"] = [cfg["prefixes"]]
    cfg["prefixes"] = [str(x).strip().lower() for x in cfg["prefixes"] if str(x).strip()]
    if not cfg["prefixes"]:
        cfg["prefixes"] = list(DEFAULT_CONFIG["prefixes"])
    # Обращение без слеша дописываем только старым файлам настроек, которые
    # о нём не знают. Ключ prefixes_migrated говорит, что автор список уже видел
    # и волен убрать оттуда что угодно.
    if isinstance(got, dict) and got.get("prefixes") and not got.get("prefixes_migrated"):
        for extra in ("виора", "viora"):
            if extra not in cfg["prefixes"]:
                cfg["prefixes"].append(extra)
    if cfg["mode"] not in MODES:
        cfg["mode"] = "draft"
    if str(cfg.get("trust") or "").lower() not in TRUST_LEVELS:
        cfg["trust"] = "read"
    return cfg


def save_config(cfg):
    ensure_home()
    body = dict(cfg)
    body["prefixes_migrated"] = True
    write_atomic(paths()["config"], json.dumps(body, ensure_ascii=False, indent=2) + "\n")


def blank_state():
    return {"version": STATE_VERSION, "cursor": 0, "seq": 0, "items": []}


def load_state():
    st = read_json(paths()["state"], None)
    if not isinstance(st, dict) or "items" not in st:
        return blank_state()
    st.setdefault("version", STATE_VERSION)
    st.setdefault("cursor", 0)
    st.setdefault("seq", 0)
    if not isinstance(st["items"], list):
        st["items"] = []
    return st


def save_state(st):
    ensure_home()
    st["items"] = st["items"][-200:]
    write_atomic(paths()["state"], json.dumps(st, ensure_ascii=False, indent=2) + "\n")


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


# --------------------------------------------------------------- разбор команд

def strip_prefix(text, cfg):
    """Отрезать префикс. Вернуть хвост или None, если это не команда моста."""
    body = (text or "").strip()
    if not body:
        return None
    low = body.lower()
    for prefix in cfg["prefixes"]:
        if low == prefix:
            return ""
        if low.startswith(prefix):
            nxt = body[len(prefix):]
            if nxt[:1] in (" ", "\n", "\t", ":", ",", ".", "!", "-"):
                return nxt.lstrip(" \n\t:,.!-")
    return None


def match_command(tail, words):
    """Слово или фраза набора в начале заявки. Вернёт (нашлось, хвост).

    За словом обязателен разделитель или конец строки: "решительный шаг" это
    заявка на пост, а не просьба решить. Фразы длиннее проверяем первыми,
    иначе "что тут" никогда не доберётся до своего вида.
    """
    body = " ".join((tail or "").split())
    low = body.lower()
    if not low:
        return False, ""
    for word in sorted(words, key=len, reverse=True):
        if low == word:
            return True, ""
        if low.startswith(word):
            nxt = body[len(word):]
            if nxt[:1] in (" ", ",", ":", ".", "!", "?", ";", "-"):
                return True, nxt.lstrip(" ,:.!?;-")
    return False, ""


def starts_with_marker(text, cfg):
    """Своё сообщение агента. Такое в ящик не кладём никогда.

    Свои тексты всегда начинаются с метки капсом и капсового слова за ней:
    VIORA ЧЕРНОВИК, VIORA ВОПРОС, VIORA НА МЕСТЕ. Обращение автора «viora реши»
    или «Viora, погода» пишется строчными, поэтому за своё не принимается.
    """
    body = (text or "").lstrip()
    marker = str(cfg.get("marker") or "VIORA")
    if not body.startswith(marker):
        return False
    rest = body[len(marker):]
    if not rest.strip():
        return True
    if rest[:1] not in (" ", "\n", "\t"):
        return False
    word = rest.split(None, 1)[0].strip(".,:!?")
    return bool(word) and word == word.upper() and any(ch.isalpha() for ch in word)


def decide_capture(text, parent_text, cfg):
    """Что делать сторожу с исходящим сообщением: cmd, answer или ничего.

    Одна точка правды для сторожа и для тестов. Правило простое: своё не берём,
    команду с префиксом берём всегда, ответ реплаем на своё сообщение берём,
    если это разрешено настройкой.
    """
    if starts_with_marker(text, cfg):
        return None
    if strip_prefix(text, cfg) is not None:
        return "cmd"
    if cfg.get("reply_capture") and parent_text and starts_with_marker(parent_text, cfg):
        if (text or "").strip():
            return "answer"
    return None


def classify(text, cfg, kind_hint="cmd", media=None):
    """Из текста автора сделать команду моста.

    Порядок важен. Сначала проверка связи, потом бытовые команды с фразами,
    потом старые слова с хвостом и слова без хвоста, и только потом заявка
    на пост. Поле media есть всегда: без файлов это пустой список, а с фото
    задачи в нём лежат пути к скачанным картинкам.
    """
    files = [str(x) for x in (media or [])]
    if kind_hint == "answer":
        return {"kind": "answer", "arg": (text or "").strip(), "media": files}
    tail = strip_prefix(text, cfg)
    if tail is None:
        return {"kind": "skip", "arg": "", "media": files}
    if not tail:
        return {"kind": "help", "arg": "", "media": files}
    parts = tail.split(None, 1)
    head = parts[0].strip().strip("/!.,:;").lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    short = " ".join(tail.split()).strip("!?.,;:() ").lower()
    if short in PING:
        return {"kind": "ping", "arg": short, "media": files}
    found, extra = match_command(tail, REMIND)
    if found and extra:
        return {"kind": "remind", "arg": extra, "media": files}
    found, extra = match_command(tail, SOLVE)
    if found:
        return {"kind": "solve", "arg": extra, "media": files}
    found, extra = match_command(tail, WEATHER)
    if found:
        return {"kind": "weather", "arg": extra, "media": files}
    found, extra = match_command(tail, PC)
    if found and not extra:
        return {"kind": "pc", "arg": "", "media": files}
    found, extra = match_command(tail, MORNING)
    if found and not extra:
        return {"kind": "morning", "arg": "", "media": files}
    for kind, words in WITH_TAIL.items():
        if head in words:
            return {"kind": kind, "arg": rest, "media": files}
    for kind, words in SOLO.items():
        if head in words and not rest:
            return {"kind": kind, "arg": "", "media": files}
    return {"kind": "request", "arg": tail, "media": files}


# ------------------------------------------------------------------------ ящик

def read_inbox():
    path = paths()["inbox"]
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("msg_id"), int):
                    out.append(row)
    except OSError:
        return []
    out.sort(key=lambda r: r["msg_id"])
    return out


def append_inbox(rows):
    """Дописать заявки в ящик, без повторов по msg_id."""
    if not rows:
        return 0
    ensure_home()
    path = paths()["inbox"]
    known = set(r["msg_id"] for r in read_inbox())
    fresh = []
    for row in rows:
        mid = row.get("msg_id")
        if not isinstance(mid, int) or mid in known:
            continue
        known.add(mid)
        row.setdefault("ts", round(time.time(), 2))
        row.setdefault("kind", "cmd")
        if not isinstance(row.get("media"), list):
            row["media"] = []
        fresh.append(row)
    if not fresh:
        return 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        for row in fresh:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    trim_inbox()
    return len(fresh)


def trim_inbox():
    rows = read_inbox()
    if len(rows) <= INBOX_MAX:
        return
    keep = rows[-INBOX_KEEP:]
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep)
    write_atomic(paths()["inbox"], body)


def mark_handled(msg_id, by="watch"):
    """Сторож отработал строку сам: агенту через next её не отдавать."""
    rows = read_inbox()
    changed = False
    for row in rows:
        if row.get("msg_id") == int(msg_id) and not row.get("handled"):
            row["handled"] = by
            changed = True
    if changed:
        write_atomic(paths()["inbox"],
                     "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return changed


def unmark_handled(msg_id):
    """Сторож взялся, но не справился: строка снова достаётся агенту через next."""
    rows = read_inbox()
    changed = False
    for row in rows:
        if row.get("msg_id") == int(msg_id) and row.get("handled"):
            row.pop("handled", None)
            changed = True
    if changed:
        write_atomic(paths()["inbox"],
                     "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return changed


# ------------------------------------------------------- один процесс в сети
# Телеграм не любит два живых подключения на одной строке сессии, а сторож
# держит соединение всегда. Поэтому сеть у сторожа одна, а остальные команды
# передают ему записки через outbox и ждут ответа с msg_id.

def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


def append_jsonl(path, row):
    ensure_home()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_lock():
    """Кто держит соединение: pid, время сердцебиения и версия сторожа."""
    row = read_json(paths()["lock"], None)
    if not isinstance(row, dict):
        return None
    try:
        row["pid"] = int(row.get("pid") or 0)
        row["heartbeat"] = float(row.get("heartbeat") or 0)
    except (TypeError, ValueError):
        return None
    return row


def write_lock(pid=None, extra=None):
    """Сторож пишет lock при старте и обновляет сердцебиение раз в несколько секунд."""
    row = {"pid": int(pid or os.getpid()), "heartbeat": round(time.time(), 2),
           "started": now_iso(), "version": VERSION}
    old = read_lock()
    if old and old.get("pid") == row["pid"] and old.get("started"):
        row["started"] = old["started"]
    if extra:
        row.update(extra)
    write_atomic(paths()["lock"], json.dumps(row, ensure_ascii=False) + "\n")
    return row


def clear_lock(pid=None):
    row = read_lock()
    if row and pid and row.get("pid") != int(pid):
        return False
    try:
        os.unlink(paths()["lock"])
    except OSError:
        return False
    return True


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def watcher_alive(now=None, ttl=HEARTBEAT_TTL):
    """Жив ли сторож: свежее сердцебиение или хотя бы живой pid."""
    row = read_lock()
    if not row:
        return False, None
    now = time.time() if now is None else now
    fresh = (now - row["heartbeat"]) <= ttl
    if fresh:
        return True, row
    # После сна компьютера сердцебиение старое, а процесс живой и скоро проснётся.
    return pid_alive(row["pid"]), row


def outbox_put(text, to="me", reply_to=0, at=None, req="", role="send"):
    """Записка сторожу: отправь это и подтверди msg_id."""
    row = {
        "id": "o%d-%d" % (int(time.time() * 1000), os.getpid()),
        "to": to or "me",
        "text": text,
        "reply_to": int(reply_to or 0),
        "at": at.isoformat() if hasattr(at, "isoformat") else (at or ""),
        "req": req or "",
        "role": role or "send",
        "ts": round(time.time(), 2),
    }
    append_jsonl(paths()["outbox"], row)
    return row


def outbox_pending(done_ids=None):
    """Строки outbox, которые сторож ещё не отработал."""
    done = done_ids if done_ids is not None else set(
        r.get("id") for r in read_jsonl(paths()["outbox_done"]))
    return [r for r in read_jsonl(paths()["outbox"]) if r.get("id") not in done]


def outbox_done(row, msg_id=0, error="", link=""):
    out = {"id": row.get("id"), "req": row.get("req", ""), "role": row.get("role", ""),
           "to": row.get("to", "me"), "msg_id": int(msg_id or 0), "link": link or "",
           "error": error or "", "ts": round(time.time(), 2)}
    append_jsonl(paths()["outbox_done"], out)
    trim_outbox()
    return out


def trim_outbox():
    """Обе ленты растут вечно, поэтому режем по хвосту, когда done уже подтвердил."""
    done_rows = read_jsonl(paths()["outbox_done"])
    if len(done_rows) <= OUTBOX_KEEP * 2:
        return
    keep = done_rows[-OUTBOX_KEEP:]
    keep_ids = set(r.get("id") for r in keep)
    write_atomic(paths()["outbox_done"],
                 "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
    done_ids = set(r.get("id") for r in done_rows)
    pending = [r for r in read_jsonl(paths()["outbox"]) if r.get("id") not in done_ids]
    tail = [r for r in read_jsonl(paths()["outbox"]) if r.get("id") in keep_ids]
    write_atomic(paths()["outbox"],
                 "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in tail + pending))


def outbox_wait(row_id, timeout=OUTBOX_WAIT, sleeper=None):
    """Ждать подтверждения от сторожа. Вернёт строку done или None."""
    sleeper = sleeper or SLEEPER
    deadline = time.time() + max(1, int(timeout))
    while True:
        for done in read_jsonl(paths()["outbox_done"]):
            if done.get("id") == row_id:
                return done
        if time.time() >= deadline:
            return None
        sleeper(0.5)


# Точка подмены для тестов: ожидание подтверждения без настоящих пауз.
SLEEPER = time.sleep


def send_via_watch(text, where, reply_to, at, cfg, req="", role="send", timeout=OUTBOX_WAIT):
    """Отправка через сторожа. NetProblem, если он не ответил вовремя."""
    row = outbox_put(text, where, reply_to, at, req, role)
    done = outbox_wait(row["id"], timeout)
    if done is None:
        raise NetProblem("сторож не подтвердил отправку за %d с, смотри %s"
                         % (timeout, paths()["log"]))
    if done.get("error"):
        raise NetProblem(done["error"])
    msg_id = int(done.get("msg_id") or 0)
    entity = done.get("to") or target_of(where, cfg)
    return {"msg_id": msg_id, "to": entity, "link": done.get("link") or link_for(entity, msg_id),
            "scheduled": bool(at), "chars": len((text or "").rstrip("\n")), "via": "watch"}


def media_size():
    """Сколько весят скачанные вложения: doctor это показывает."""
    root = paths()["media"]
    total, count = 0, 0
    for base, _dirs, names in os.walk(root):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(base, name))
                count += 1
            except OSError:
                continue
    return total, count


def clean_media(days=14, now=None):
    """Папки вложений старше двух недель уходят: задача давно решена."""
    root = paths()["media"]
    if not os.path.isdir(root):
        return 0
    now = time.time() if now is None else now
    limit = now - days * 86400
    gone = 0
    for name in os.listdir(root):
        full = os.path.join(root, name)
        try:
            stamp = os.path.getmtime(full)
        except OSError:
            continue
        if stamp >= limit:
            continue
        import shutil
        shutil.rmtree(full, ignore_errors=True)
        if not os.path.exists(full):
            gone += 1
    return gone


MCP_ID = re.compile(r"^ID:\s*(\d+)\b")
MCP_DATE = re.compile(r"\|\s*Date:\s*([^|]+)")
MCP_SPLIT = " | Message: "


def parse_mcp(text):
    """Разобрать вывод list_messages из MCP в строки ящика.

    Формат строки MCP: ID: 12 | ... | Date: ... | Message: текст, переносы
    внутри текста экранированы в \\n.
    """
    out = []
    for raw in (text or "").split("\n"):
        raw = raw.strip()
        m = MCP_ID.match(raw)
        if not m:
            continue
        cut = raw.find(MCP_SPLIT)
        if cut == -1:
            continue
        body = raw[cut + len(MCP_SPLIT):]
        body = body.replace("\\n", "\n")
        if body.strip() == "[empty]":
            body = ""
        date = ""
        dm = MCP_DATE.search(raw[:cut])
        if dm:
            date = dm.group(1).strip()
        out.append({"msg_id": int(m.group(1)), "date": date, "text": body,
                    "src": "poll", "kind": "cmd"})
    return out


def feed_rows(text, cfg=None):
    """Строки из вывода MCP, которые реально стоит положить в ящик."""
    cfg = cfg or load_config()
    return [r for r in parse_mcp(text) if decide_capture(r["text"], "", cfg) == "cmd"]


# --------------------------------------------------------------------- заявки

def find(st, item_id):
    for item in st["items"]:
        if item["id"] == item_id:
            return item
    return None


def active(st):
    """Живые заявки: те, что ещё не закрыты."""
    return [i for i in st["items"] if i["status"] in ("work", "asked", "staged", "await")]


def awaiting(st):
    for item in reversed(st["items"]):
        if item["status"] in ("await", "staged"):
            return item
    return None


def new_item(st, row, raw):
    st["seq"] = int(st.get("seq", 0)) + 1
    item = {
        "id": "r%d" % st["seq"],
        "msg_id": row.get("msg_id"),
        "date": row.get("date", ""),
        "created": now_iso(),
        "updated": now_iso(),
        "status": "work",
        "raw": raw,
        "brief": brief_of(raw),
        "rubric": "",
        "draft": "",
        "draft_msg_id": 0,
        "ask_msg_id": 0,
        "published_msg_id": 0,
        "link": "",
        "note": "",
        "tries": 0,
    }
    st["items"].append(item)
    return item


def item_by_msg(st, msg_id):
    """Заявка, заведённая по этому сообщению автора. Сторож мог открыть её раньше агента."""
    if not msg_id:
        return None
    for item in reversed(st["items"]):
        if item.get("msg_id") == msg_id and item.get("status") not in ("published", "scheduled", "cancelled"):
            return item
    return None


def brief_of(text):
    one = " ".join((text or "").split())
    return one[:BRIEF]


def touch(item, **fields):
    item.update(fields)
    item["updated"] = now_iso()


def target_for(st, row, cmd):
    """К какой заявке относится короткая команда автора."""
    parent = row.get("reply_to")
    if parent:
        for item in reversed(st["items"]):
            if parent in (item.get("draft_msg_id"), item.get("ask_msg_id")):
                return item
    if cmd["kind"] in ("publish", "schedule"):
        return awaiting(st)
    live = active(st)
    return live[-1] if live else None


# ----------------------------------------------------------------- next и feed

def fresh_rows(st=None):
    """Строки ящика, которые агент ещё не забрал и которые сторож не закрыл сам."""
    st = st or load_state()
    cursor = int(st.get("cursor", 0))
    return [r for r in read_inbox()
            if int(r.get("msg_id", 0)) > cursor and not r.get("handled")]


def take_next(st, cfg):
    """Первая необработанная строка ящика. Курсор двигаем сразу.

    Строки с пометкой handled сторож уже отработал сам (комп, погода, решение,
    черновик от мозга): агенту их не отдаём, но курсор через них проводим.
    """
    for row in read_inbox():
        if int(row.get("msg_id", 0)) <= int(st.get("cursor", 0)):
            continue
        st["cursor"] = int(row["msg_id"])
        if row.get("handled"):
            save_state(st)
            continue
        cmd = classify(row.get("text", ""), cfg, row.get("kind", "cmd"),
                       row.get("media") or [])
        if cmd["kind"] == "skip":
            save_state(st)
            continue
        if cmd["kind"] == "request":
            item = item_by_msg(st, row.get("msg_id")) or new_item(st, row, cmd["arg"])
            save_state(st)
            return {"kind": "request", "id": item["id"], "msg_id": row.get("msg_id"),
                    "text": cmd["arg"], "rubric": item.get("rubric") or cfg.get("rubric_default", ""),
                    "media": list(row.get("media") or []),
                    "mode": cfg["mode"], "source": row.get("src", "watch")}
        item = target_for(st, row, cmd)
        if cmd["kind"] in ("edit", "answer") and item is not None:
            touch(item, status="work", note=cmd["arg"][:200])
        if cmd["kind"] == "cancel" and item is not None:
            touch(item, status="cancelled")
        save_state(st)
        return {"kind": cmd["kind"], "id": item["id"] if item else "",
                "msg_id": row.get("msg_id"), "text": cmd["arg"],
                "media": list(row.get("media") or []),
                "mode": cfg["mode"], "source": row.get("src", "watch")}
    return None


def resume_candidate(st):
    for item in active(st):
        if int(item.get("tries", 0)) >= MAX_TRIES:
            continue
        if item["status"] in ("work", "staged"):
            touch(item, tries=int(item.get("tries", 0)) + 1)
            save_state(st)
            return {"kind": "resume", "id": item["id"], "msg_id": item.get("msg_id"),
                    "text": item.get("raw", ""), "status": item["status"],
                    "note": item.get("note", "")}
    return None


def emit(result, as_json):
    result.setdefault("hint", HINTS.get(result.get("kind", ""), ""))
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("команда: %s" % result.get("kind"))
    if result.get("id"):
        print("заявка: %s" % result["id"])
    if result.get("text"):
        print("текст: %s" % result["text"])
    if result.get("hint"):
        print("что делать: %s" % result["hint"])
    return 0


def cmd_next(args):
    cfg = load_config()
    wait = max(0, min(int(args.wait or 0), MAX_WAIT))
    deadline = time.time() + wait
    checked_resume = False
    while True:
        st = load_state()
        got = take_next(st, cfg)
        if got:
            return emit(got, args.as_json)
        if not checked_resume:
            checked_resume = True
            got = resume_candidate(load_state())
            if got:
                return emit(got, args.as_json)
        if time.time() >= deadline:
            return emit({"kind": "empty"}, args.as_json)
        time.sleep(1.0)


def cmd_feed(args):
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            raw = handle.read()
    else:
        raw = sys.stdin.read()
    cfg = load_config()
    rows = feed_rows(raw, cfg)
    added = append_inbox(rows)
    if args.as_json:
        print(json.dumps({"parsed": len(rows), "added": added}, ensure_ascii=False))
    else:
        print("строк с командой: %d, новых в ящике: %d" % (len(rows), added))
    return 0


# -------------------------------------------------------- черновик и публикация

def lint(path, rubric, media):
    argv = [sys.executable, os.path.join(HERE, "lint_post.py"), path, "--strict"]
    if rubric:
        argv += ["--rubric", rubric]
    if media:
        argv.append("--media")
    done = subprocess.run(argv, capture_output=True, text=True)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def draft_message(item, cfg, text):
    head = "%s ЧЕРНОВИК %s" % (cfg["marker"], item["id"])
    facts = "рубрика %s, знаков %d" % (item.get("rubric") or "не задана", len(text))
    if cfg["mode"] == "auto":
        ask = "режим автопубликации: уходит в канал без подтверждения"
    elif cfg["mode"] == "scheduled":
        ask = "%s пуб поставлю в отложенные, %s правь скажи что не так" % (cfg["prefixes"][0], cfg["prefixes"][0])
    else:
        ask = "%s пуб публикую, %s правь скажи что не так" % (cfg["prefixes"][0], cfg["prefixes"][0])
    return "\n".join([head, facts, ask, "", text])


def cmd_stage(args):
    st = load_state()
    item = find(st, args.req)
    if item is None:
        print("нет такой заявки: %s" % args.req)
        return 2
    if not os.path.exists(args.file):
        print("нет файла черновика: %s" % args.file)
        return 2
    with open(args.file, encoding="utf-8") as handle:
        text = handle.read().strip("\n")
    if not text.strip():
        print("черновик пустой")
        return 2
    rubric = args.rubric or item.get("rubric") or load_config().get("rubric_default", "")
    code, output = lint(args.file, rubric, args.media)
    if code != 0:
        print("линтер не пустил черновик, правь по подсказкам и запускай stage снова")
        print(output.strip())
        return 1
    ensure_home()
    dest = os.path.join(paths()["drafts"], "%s.txt" % item["id"])
    write_atomic(dest, text + "\n")
    touch(item, status="staged", draft=dest, rubric=rubric)
    save_state(st)
    cfg = load_config()
    card = draft_message(item, cfg, text)
    if not getattr(args, "send", False):
        print(card)
        return 0
    try:
        out = send_text(card, "me", int(item.get("msg_id") or 0), None, cfg,
                        req=item["id"], role="draft")
    except NetProblem as err:
        print(card)
        print("")
        print("в Телеграм не ушло: %s" % err)
        return 1
    # Сторож мог уже отметить заявку сам: перечитываем состояние, а не пишем поверх.
    st = load_state()
    item = find(st, item["id"]) or item
    touch(item, status="await", draft_msg_id=out["msg_id"], tries=0)
    save_state(st)
    print(card)
    print("")
    print("черновик ушёл автору: id %d" % out["msg_id"])
    return 0


def cmd_sent(args):
    st = load_state()
    item = find(st, args.req)
    if item is None:
        print("нет такой заявки: %s" % args.req)
        return 2
    touch(item, status="await", draft_msg_id=int(args.msg_id), tries=0)
    save_state(st)
    print("заявка %s ждёт добро автора" % item["id"])
    return 0


def cmd_payload(args):
    st = load_state()
    item = find(st, args.req)
    if item is None:
        print("нет такой заявки: %s" % args.req)
        return 2
    path = item.get("draft")
    if not path or not os.path.exists(path):
        print("черновик не сохранён, сначала stage")
        return 1
    with open(path, encoding="utf-8") as handle:
        text = handle.read().strip("\n")
    if args.out:
        write_atomic(args.out, text + "\n", 0o644)
        print(args.out)
        return 0
    sys.stdout.write(text + "\n")
    return 0


def cmd_ask(args):
    st = load_state()
    item = find(st, args.req)
    if item is None:
        print("нет такой заявки: %s" % args.req)
        return 2
    cfg = load_config()
    question = " ".join((args.text or "").split())
    if not question:
        print("дай текст вопроса")
        return 2
    touch(item, status="asked", note=question[:200])
    save_state(st)
    card = "%s ВОПРОС %s\n%s" % (cfg["marker"], item["id"], question)
    if not getattr(args, "send", False):
        print(card)
        return 0
    try:
        out = send_text(card, "me", int(item.get("msg_id") or 0), None, cfg,
                        req=item["id"], role="ask")
    except NetProblem as err:
        print(card)
        print("")
        print("в Телеграм не ушло: %s" % err)
        return 1
    st = load_state()
    item = find(st, item["id"]) or item
    touch(item, ask_msg_id=out["msg_id"])
    save_state(st)
    print(card)
    print("")
    print("вопрос ушёл автору: id %d" % out["msg_id"])
    return 0


def cmd_asked(args):
    st = load_state()
    item = find(st, args.req)
    if item is None:
        print("нет такой заявки: %s" % args.req)
        return 2
    touch(item, ask_msg_id=int(args.msg_id))
    save_state(st)
    print("вопрос по %s отмечен" % item["id"])
    return 0


def cmd_published(args):
    st = load_state()
    item = find(st, args.req)
    if item is None:
        print("нет такой заявки: %s" % args.req)
        return 2
    cfg = load_config()
    link = args.link or ""
    status = "scheduled" if args.scheduled else "published"
    touch(item, status=status, published_msg_id=int(args.msg_id or 0), link=link)
    save_state(st)
    if args.topic:
        log_post(item, args.topic, args.services, link)
    word = "ОТЛОЖЕН" if args.scheduled else "ГОТОВО"
    tail = link if link else "ссылки нет, канал приватный"
    print("%s %s %s\n%s" % (cfg["marker"], word, item["id"], tail))
    return 0


def log_post(item, topic, services, link):
    argv = [sys.executable, os.path.join(HERE, "memory.py"), "add-post",
            "--type", item.get("rubric") or "абуз", "--topic", topic]
    if services:
        argv += ["--services", services]
    if link:
        argv += ["--url", link]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def cmd_cancel(args):
    st = load_state()
    item = find(st, args.req)
    if item is None:
        print("нет такой заявки: %s" % args.req)
        return 2
    touch(item, status="cancelled", note=(args.why or "")[:200])
    save_state(st)
    cfg = load_config()
    print("%s ОТМЕНА %s" % (cfg["marker"], item["id"]))
    return 0


# ------------------------------------------------------------- тексты для автора

def card_text(cfg=None):
    cfg = cfg or load_config()
    p = cfg["prefixes"][0]
    lines = [
        "%s ПОМОЩЬ" % cfg["marker"],
        "Пишу посты по заявкам из этого чата. Всё, что без %s, я не читаю." % p,
        "",
        "%s текст или ссылка   новая заявка на пост" % p,
        "%s правь что не так   переделать черновик" % p,
        "%s пуб                опубликовать в канал" % p,
        "%s отложи 20:30       поставить в отложенные" % p,
        "%s стоп               отменить заявку" % p,
        "%s что                что сейчас в работе" % p,
        "%s помощь             эта карточка" % p,
        "",
        "Без поста, сразу ответом:",
        "%s реши задачу       решить или объяснить, можно фотом" % p,
        "%s пк                 место на диске, память, батарея" % p,
        "%s погода             погода по городу из профиля" % p,
        "%s утро               утренний дайджест одним сообщением" % p,
        "%s напомни 08:00 утро  добавить в расписание" % p,
        "",
        "Можно без слеша: виора реши, виора погода, виора утро.",
        "Можно кинуть фото или альбом и написать заявку в подписи.",
        "На мой вопрос отвечай реплаем, префикс не нужен.",
        "Файлы на компе я не трогаю и команд не выполняю: только то, что в этой карточке.",
    ]
    return "\n".join(lines)


def status_text(cfg=None):
    cfg = cfg or load_config()
    st = load_state()
    lines = ["%s СТАТУС" % cfg["marker"]]
    live = active(st)
    if live:
        for item in live[-3:]:
            lines.append("%s %s: %s" % (item["id"], human_status(item["status"]), item["brief"] or "без темы"))
    else:
        lines.append("в работе ничего, ящик чист")
    done = [i for i in st["items"] if i["status"] in ("published", "scheduled")]
    if done:
        last = done[-1]
        lines.append("последний: %s %s %s" % (last["id"], human_status(last["status"]),
                                              last.get("link") or ""))
    fresh = [r for r in read_inbox() if int(r.get("msg_id", 0)) > int(st.get("cursor", 0))]
    lines.append("новых заявок в ящике: %d" % len(fresh))
    return "\n".join(x.rstrip() for x in lines)


def human_status(code):
    return {
        "work": "пишу",
        "asked": "жду ответ",
        "staged": "черновик готов",
        "await": "жду добро",
        "published": "опубликован",
        "scheduled": "в отложенных",
        "cancelled": "отменён",
    }.get(code, code)


def cmd_card(args):
    print(card_text())
    return 0


def cmd_status(args):
    if args.as_json:
        st = load_state()
        print(json.dumps({"cursor": st["cursor"], "items": st["items"][-10:]},
                         ensure_ascii=False, indent=2))
        return 0
    print(status_text())
    return 0


# ------------------------------------------------------------------- время окна

def window_bounds(cfg):
    raw = str(cfg.get("window") or "16:30-21:00")
    try:
        a, b = raw.split("-", 1)
        ah, am = [int(x) for x in a.strip().split(":")]
        bh, bm = [int(x) for x in b.strip().split(":")]
        return (ah, am), (bh, bm)
    except (ValueError, TypeError):
        return (16, 30), (21, 0)


def tzinfo_of(cfg):
    name = str(cfg.get("tz") or "Europe/Astrakhan")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=4))


def next_slot(cfg, at=None, now=None):
    """Когда публиковать: своё окно канала, а не случайный час ночи."""
    tz = tzinfo_of(cfg)
    now = now or datetime.datetime.now(tz)
    start, end = window_bounds(cfg)
    if at:
        hh, mm = [int(x) for x in at.split(":")]
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return target
    begin = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    finish = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
    if now < begin:
        return begin
    if now <= finish - datetime.timedelta(minutes=5):
        return now + datetime.timedelta(minutes=10)
    return begin + datetime.timedelta(days=1)


def cmd_when(args):
    cfg = load_config()
    try:
        when = next_slot(cfg, args.at)
    except (ValueError, TypeError):
        print("время не разобрал, пиши так: --at 20:30")
        return 2
    if args.as_json:
        print(json.dumps({"iso": when.isoformat(), "tz": cfg.get("tz"),
                          "window": cfg.get("window")}, ensure_ascii=False))
    else:
        print(when.isoformat())
    return 0


# ---------------------------------------------------------------- доктор и конфиг

def cmd_config(args):
    cfg = load_config()
    changed = []
    for pair in args.set or []:
        if "=" not in pair:
            print("настройка задаётся так: --set channel=@VioraStudio")
            return 2
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in DEFAULT_CONFIG:
            print("нет такой настройки: %s" % key)
            return 2
        if key == "prefixes":
            cfg[key] = [x.strip().lower() for x in value.split(",") if x.strip()]
        elif key == "reply_capture":
            cfg[key] = value.lower() in ("1", "да", "true", "yes", "on")
        elif key == "mode":
            if value not in MODES:
                print("режим бывает только draft, scheduled или auto")
                return 2
            cfg[key] = value
        elif key == "trust":
            if value.lower() not in TRUST_LEVELS:
                print("trust бывает только read или edit")
                return 2
            cfg[key] = value.lower()
        else:
            cfg[key] = value
        changed.append(key)
    if changed:
        save_config(cfg)
    if args.as_json:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0
    print("рабочая папка: %s" % paths()["home"])
    for key in sorted(cfg):
        print("  %-14s %s" % (key, cfg[key]))
    if changed:
        print("обновлено: %s" % ", ".join(changed))
    return 0


def telethon_ready():
    try:
        import telethon  # noqa: F401
        return True
    except Exception:
        return False


def watch_session():
    for name in ("TELEGRAM_SESSION_STRING_WATCH", "TELEGRAM_WATCH_SESSION_STRING"):
        value = os.environ.get(name)
        if value:
            return name, value
    value = os.environ.get("TELEGRAM_SESSION_STRING")
    if value:
        return "TELEGRAM_SESSION_STRING", value
    return "", ""


# --------------------------------------------------------------- сеть: телетон
# Почему сеть живёт здесь, а не в стороннем сервере. Сервер MCP был лишним
# звеном: если он не поднялся или агент не позвал инструмент, автор видел
# тишину в Избранном и никто не мог сказать, на каком шаге всё встало. Теперь
# отправляет тот же скрипт, который ведёт состояние. Одна команда, один ответ,
# одна понятная ошибка.

class NetProblem(Exception):
    """Причина отказа человеческими словами."""


def secrets_dirs():
    """Где искать ключи: переменная, корень поставки, сам скилл, текущая папка."""
    out = []
    env = os.environ.get("VIORA_SECRETS")
    if env:
        # Явно указанная папка главнее всего: иначе тест или вторая установка
        # незаметно зацепит чужие ключи.
        return [os.path.abspath(os.path.expanduser(env))]
    out.append(os.path.join(os.path.dirname(SKILL), "secrets"))
    out.append(os.path.join(SKILL, "secrets"))
    out.append(os.path.join(os.getcwd(), "secrets"))
    uniq = []
    for path in out:
        if path not in uniq:
            uniq.append(path)
    return uniq


def read_env_file(path):
    """Строки вида КЛЮЧ=ЗНАЧЕНИЕ. Решётка это заметка, а не ключ."""
    rows = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.lstrip("\ufeff").strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                rows[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return rows


def secrets_env():
    """Ключи из secrets/*.env. Переменные окружения важнее файла."""
    merged = {}
    for folder in secrets_dirs():
        for name in SECRET_FILES:
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            for key, value in read_env_file(path).items():
                if value and key not in merged:
                    merged[key] = value
    return merged


def secrets_file():
    """Куда писать строку сессии: в тот watch.env, который уже лежит на диске."""
    folders = secrets_dirs()
    for folder in folders:
        path = os.path.join(folder, "watch.env")
        if os.path.isfile(path):
            return path
    folder = folders[0] if folders else os.getcwd()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = os.getcwd()
    return os.path.join(folder, "watch.env")


def creds():
    """Ключи и строка сессии плюс словами, чего не хватает."""
    disk = secrets_env()
    api_id = (os.environ.get("TELEGRAM_API_ID") or disk.get("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.environ.get("TELEGRAM_API_HASH") or disk.get("TELEGRAM_API_HASH") or "").strip()
    session, source = "", ""
    for name in SESSION_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            session, source = value, name
            break
    if not session:
        for name in SESSION_ENVS:
            value = (disk.get(name) or "").strip()
            if value:
                session, source = value, "secrets: %s" % name
                break
    problems = []
    if not api_id.isdigit():
        problems.append("нет ключа TELEGRAM_API_ID")
    if not api_hash:
        problems.append("нет ключа TELEGRAM_API_HASH")
    if not session:
        problems.append("нет строки сессии: запусти python3 tools/tg.py login")
    return {"api_id": api_id, "api_hash": api_hash, "session": session,
            "source": source, "problems": problems}


def save_session(value):
    """Строка сессии ложится в secrets/watch.env сама, копировать нечего."""
    path = secrets_file()
    info = creds()
    rows = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as handle:
            rows = handle.read().splitlines()
    pairs = []
    if info["api_id"]:
        pairs.append(("TELEGRAM_API_ID", info["api_id"]))
    if info["api_hash"]:
        pairs.append(("TELEGRAM_API_HASH", info["api_hash"]))
    pairs.append((SESSION_KEY, value))
    for key, val in pairs:
        line = "%s=%s" % (key, val)
        placed = False
        for index, row in enumerate(rows):
            clean = row.lstrip("\ufeff").strip()
            if clean.startswith(key + "=") or clean.startswith(key + " ="):
                rows[index] = line
                placed = True
                break
        if not placed:
            rows.append(line)
    body = "\n".join(row.lstrip("\ufeff") for row in rows).strip() + "\n"
    write_atomic(path, body)
    return path


def telethon_bits():
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError:
        raise NetProblem("нет telethon: поставь его командой pip install telethon")
    return TelegramClient, StringSession


def connect():
    """Подключённый клиент. Любой отказ объясняем одной строкой."""
    info = creds()
    if info["problems"]:
        raise NetProblem("; ".join(info["problems"]))
    TelegramClient, StringSession = telethon_bits()
    client = TelegramClient(
        StringSession(info["session"]), int(info["api_id"]), info["api_hash"],
        device_model=os.environ.get("TELEGRAM_DEVICE_MODEL", DEVICE),
        system_version=os.environ.get("TELEGRAM_SYSTEM_VERSION", "1.0"),
        app_version=os.environ.get("TELEGRAM_APP_VERSION", VERSION),
    )
    try:
        client.connect()
    except Exception as err:
        raise NetProblem("нет связи с Телеграмом: %s" % err)
    if not client.is_user_authorized():
        try:
            client.disconnect()
        except Exception:
            pass
        raise NetProblem("строка сессии не подошла: python3 tools/tg.py login --force")
    return client


def target_of(where, cfg):
    """Куда отправлять: me это Избранное, channel это канал из настроек."""
    name = (where or "me").strip()
    if name.lower() in ("me", "self", "saved", "избранное", "себе"):
        return "me"
    if name.lower() in ("channel", "канал"):
        name = str(cfg.get("channel") or "").strip()
    if not name:
        raise NetProblem("не задан канал: python3 tools/tg.py config --set channel=@VioraStudio")
    return name


def link_for(where, msg_id):
    """Ссылка на сообщение, если канал публичный."""
    name = str(where or "").strip()
    if not name.startswith("@") or not msg_id:
        return ""
    return "https://t.me/%s/%d" % (name.lstrip("@"), int(msg_id))


def send_text(text, where="me", reply_to=0, at=None, cfg=None, req="", role="send",
              direct=False):
    """Отправить сообщение. Одна дорога для всех команд моста.

    Сторож жив: записка уходит в outbox, а сеть остаётся у него одного.
    Сторожа нет или direct=True: подключаемся сами, как раньше.
    """
    cfg = cfg or load_config()
    body = (text or "").rstrip("\n")
    if not body.strip():
        raise NetProblem("пустой текст, отправлять нечего")
    if len(body) > TG_LIMIT:
        raise NetProblem("текст %d знаков, предел Телеграма %d: режь через split_post.py"
                         % (len(body), TG_LIMIT))
    entity = target_of(where, cfg)
    if not direct:
        alive, _row = watcher_alive()
        if alive:
            return send_via_watch(body, entity, reply_to, at, cfg, req, role)
    return send_direct(body, entity, reply_to, at, cfg)


def send_direct(body, entity, reply_to=0, at=None, cfg=None, client=None):
    """Своё подключение к Телеграму: для команд без сторожа и для самого сторожа."""
    cfg = cfg or load_config()
    own = client is None
    if own:
        client = connect()
    try:
        kwargs = {"parse_mode": "md", "link_preview": bool(cfg.get("preview"))}
        if reply_to:
            kwargs["reply_to"] = int(reply_to)
        if at is not None:
            kwargs["schedule"] = at
        try:
            msg = client.send_message(entity, body, **kwargs)
        except Exception as err:
            # Старое сообщение могло уйти в трубу. Текст важнее ответа на реплай.
            if "reply_to" in kwargs and "reply" in str(err).lower():
                kwargs.pop("reply_to")
                msg = client.send_message(entity, body, **kwargs)
            else:
                raise NetProblem("Телеграм отказал: %s" % err)
    finally:
        if own:
            try:
                client.disconnect()
            except Exception:
                pass
    msg_id = int(getattr(msg, "id", 0) or 0)
    return {"msg_id": msg_id, "to": entity, "link": link_for(entity, msg_id),
            "scheduled": at is not None, "chars": len(body), "via": "direct"}


def ping_text(cfg=None):
    """Ответ на проверку связи: одна строка и никакой воды."""
    cfg = cfg or load_config()
    live = active(load_state())
    return "%s НА СМЕНЕ. В работе %d, режим %s, канал %s." % (
        cfg["marker"], len(live), cfg["mode"], cfg["channel"])


def qr_lines(url):
    """QR-код как текст для консоли. Нет qrcode: вернём пустой список, URL останется."""
    try:
        import qrcode
    except ImportError:
        return []
    import io
    code = qrcode.QRCode(border=1)
    code.add_data(url)
    code.make(fit=True)
    buf = io.StringIO()
    try:
        code.print_ascii(out=buf, invert=True)
    except Exception:
        return []
    return [row for row in buf.getvalue().split("\n") if row.strip()]


# Тесты подменяют эти точки, чтобы гонять вход без сети и без телефона.
QR_PRINTER = qr_lines
PASSWORD_PROMPT = None


def ask_password():
    """Пароль облака спрашиваем без эха: он не должен остаться в истории терминала."""
    if PASSWORD_PROMPT is not None:
        return PASSWORD_PROMPT()
    import getpass
    try:
        return getpass.getpass("Пароль облака Телеграма (двухфакторка): ")
    except (EOFError, KeyboardInterrupt):
        return ""


def login_qr(client, printer=None, waiter=None, tries=6):
    """Вход по QR: показать код, ждать сканирования, обновлять по истечении.

    Возвращает объект пользователя. Двухфакторку ловим по SessionPasswordNeededError
    и спрашиваем пароль облака один раз. Ошибки узнаём по имени класса, чтобы
    тесты гоняли вход без telethon.
    """
    printer = printer or QR_PRINTER
    login = client.qr_login()
    for attempt in range(1, tries + 1):
        url = login.url
        print("")
        print("Вход по QR, попытка %d из %d." % (attempt, tries))
        print("Открой Телеграм на телефоне: Настройки, Устройства, Подключить устройство,")
        print("наведи камеру на код ниже. Код живёт около минуты, потом появится новый.")
        print("")
        for row in printer(url):
            print(row)
        print("")
        print("Та же ссылка текстом, если камеры нет: %s" % url)
        print("(открывается на телефоне или в Telegram Desktop, где вход уже есть)")
        try:
            if waiter is not None:
                return waiter(login)
            return login.wait()
        except Exception as err:
            name = type(err).__name__
            if name == "SessionPasswordNeededError":
                password = ask_password()
                if not password:
                    raise NetProblem("нужен пароль облака, без него QR-вход не завершить")
                return client.sign_in(password=password)
            if "Timeout" in name or "timeout" in str(err).lower():
                login.recreate()
                continue
            raise
    raise NetProblem("код не отсканирован за %d попыток, запусти login ещё раз" % tries)


def login_phone(client):
    """Старый путь: номер, код из Телеграма, при двухфакторке пароль."""
    print("Сейчас Телеграм спросит номер и код.")
    print("Номер с плюсом и кодом страны, например +79991234567.")
    print("Код придёт в сам Телеграм, в чат Telegram.")
    print("")
    client.start()
    return client.get_me()


def cmd_login(args):
    """Вход в аккаунт. По умолчанию QR с телефона, --phone для номера и кода."""
    info = creds()
    if not info["api_id"].isdigit() or not info["api_hash"]:
        print("нет ключей api. Впиши TELEGRAM_API_ID и TELEGRAM_API_HASH в secrets/watch.env")
        return 1
    if info["session"] and not getattr(args, "force", False):
        print("строка сессии уже есть (%s)" % (info["source"] or "secrets"))
        print("нужен новый вход: python3 tools/tg.py login --force")
        return 0
    alive, row = watcher_alive()
    if alive:
        print("сторож сейчас держит соединение (pid %s). Останови его, потом входи заново."
              % (row or {}).get("pid"))
        return 1
    try:
        TelegramClient, StringSession = telethon_bits()
    except NetProblem as err:
        print(str(err))
        return 1
    client = TelegramClient(
        StringSession(), int(info["api_id"]), info["api_hash"],
        device_model=os.environ.get("TELEGRAM_DEVICE_MODEL", DEVICE),
    )
    try:
        if getattr(args, "phone", False):
            me = login_phone(client)
        else:
            client.connect()
            me = login_qr(client)
            if me is None or not client.is_user_authorized():
                raise NetProblem("Телеграм не подтвердил вход")
            me = client.get_me()
        session = client.session.save()
    except NetProblem as err:
        print("войти не получилось: %s" % err)
        return 1
    except Exception as err:
        print("войти не получилось: %s" % err)
        print("запасной путь по номеру: python3 tools/tg.py login --phone --force")
        return 1
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    path = save_session(session)
    name = (getattr(me, "first_name", "") or "").strip()
    tag = getattr(me, "username", None)
    print("")
    print("вошёл как %s%s" % (name, " (@%s)" % tag if tag else ""))
    print("строка сессии записана: %s" % path)
    print("это полный вход в аккаунт, никому её не показывай")
    return 0


PIP_PACKAGES = ("telethon", "qrcode")


def module_ready(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def cmd_setup(args):
    """Одна команда на всю настройку: доставить, войти, показать итог."""
    print("VIORA STUDIO, настройка моста с Телеграмом")
    print("")
    missing = [name for name in PIP_PACKAGES if not module_ready(name)]
    if missing:
        print("ставлю %s..." % ", ".join(missing))
        code = subprocess.call([sys.executable, "-m", "pip", "install", "--quiet",
                               "--disable-pip-version-check"] + missing)
        still = [name for name in missing if not module_ready(name)]
        if code != 0 or "telethon" in still:
            print("telethon не встал. Поставь руками: pip install telethon qrcode")
            return 1
        if "qrcode" in still:
            print("qrcode не встал: QR покажу ссылкой, вход по ней тоже работает")
        else:
            print("зависимости на месте")
    info = creds()
    if not info["api_id"].isdigit() or not info["api_hash"]:
        print("нет ключей api. Они лежат в secrets/watch.env, впиши их туда")
        return 1
    if not info["session"]:
        code = cmd_login(argparse.Namespace(force=False, phone=bool(getattr(args, "phone", False))))
        if code != 0:
            return code
    cfg = load_config()
    print("")
    print("канал %s, режим %s, окно %s" % (cfg["channel"], cfg["mode"], cfg["window"]))
    print("всё на месте. Смена: python3 tools/tg_watch.py")
    return 0


def cmd_send(args):
    """Отправить текст себе в Избранное или в канал."""
    cfg = load_config()
    text = getattr(args, "text", "") or ""
    if getattr(args, "file", None):
        try:
            with open(args.file, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as err:
            print("не читается файл: %s" % err)
            return 2
    if not text.strip():
        print("дай текст: --text или --file")
        return 2
    at = None
    if getattr(args, "at", None):
        try:
            at = next_slot(cfg, args.at)
        except (ValueError, TypeError):
            print("время не разобрал, пиши так: --at 20:30")
            return 2
    if getattr(args, "dry_run", False):
        try:
            where = target_of(args.to, cfg)
        except NetProblem as err:
            print(str(err))
            return 1
        print(json.dumps({"dry": True, "to": where, "chars": len(text.strip()),
                          "at": at.isoformat() if at else ""}, ensure_ascii=False))
        return 0
    try:
        out = send_text(text, args.to, getattr(args, "reply_to", 0), at, cfg,
                        direct=bool(getattr(args, "direct", False)))
    except NetProblem as err:
        print(str(err))
        return 1
    if getattr(args, "as_json", False):
        print(json.dumps(out, ensure_ascii=False))
    else:
        tail = ", %s" % out["link"] if out["link"] else ""
        via = " через сторожа" if out.get("via") == "watch" else ""
        print("ушло в %s: id %d%s%s" % (out["to"], out["msg_id"], tail, via))
    return 0


def cmd_ping(args):
    """Ответить автору, что смена идёт."""
    cfg = load_config()
    text = ping_text(cfg)
    if getattr(args, "local", False):
        print(text)
        return 0
    try:
        send_text(text, "me", int(getattr(args, "reply_to", 0) or 0), None, cfg, role="ping")
    except NetProblem as err:
        print(text)
        print("в Телеграм не ушло: %s" % err)
        return 1
    print(text)
    return 0


def cmd_poll(args):
    """Забрать заявки из Избранного своими руками, без сторожа."""
    cfg = load_config()
    alive, row = watcher_alive()
    if alive:
        print("сторож жив (pid %s) и сам кладёт заявки в ящик: poll не нужен"
              % (row or {}).get("pid"))
        return 0
    try:
        client = connect()
    except NetProblem as err:
        print(str(err))
        return 1
    rows = []
    try:
        limit = max(1, min(int(getattr(args, "limit", 30) or 30), 200))
        for msg in client.iter_messages("me", limit=limit):
            text = getattr(msg, "message", "") or ""
            if not text.strip():
                continue
            parent_text = ""
            parent_id = int(getattr(msg, "reply_to_msg_id", 0) or 0)
            if parent_id and cfg.get("reply_capture"):
                try:
                    parent = client.get_messages("me", ids=parent_id)
                    parent_text = getattr(parent, "message", "") or ""
                except Exception:
                    parent_text = ""
            verdict = decide_capture(text, parent_text, cfg)
            if not verdict:
                continue
            rows.append({"msg_id": int(msg.id), "text": text, "kind": verdict,
                         "src": "poll", "date": str(getattr(msg, "date", "")),
                         "reply_to": parent_id})
    except Exception as err:
        print("чтение Избранного не удалось: %s" % err)
        return 1
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    rows.sort(key=lambda row: row["msg_id"])
    added = append_inbox(rows)
    if getattr(args, "as_json", False):
        print(json.dumps({"seen": len(rows), "added": added}, ensure_ascii=False))
    else:
        print("с командой нашлось %d, новых в ящике %d" % (len(rows), added))
    return 0


def cmd_publish(args):
    """Отправить готовый черновик в канал и закрыть заявку."""
    st = load_state()
    item = find(st, args.req)
    if item is None:
        print("нет такой заявки: %s" % args.req)
        return 2
    path = item.get("draft")
    if not path or not os.path.exists(path):
        print("черновик не сохранён, сначала stage")
        return 1
    with open(path, encoding="utf-8") as handle:
        text = handle.read().strip("\n")
    cfg = load_config()
    at = None
    if getattr(args, "at", None) or cfg["mode"] == "scheduled":
        try:
            at = next_slot(cfg, getattr(args, "at", None))
        except (ValueError, TypeError):
            print("время не разобрал, пиши так: --at 20:30")
            return 2
    try:
        out = send_text(text, getattr(args, "to", "channel") or "channel", 0, at, cfg,
                        req=item["id"], role="publish")
    except NetProblem as err:
        print("в канал не ушло: %s" % err)
        return 1
    st = load_state()
    item = find(st, item["id"]) or item
    touch(item, status="scheduled" if at else "published",
          published_msg_id=out["msg_id"], link=out["link"])
    save_state(st)
    if getattr(args, "topic", ""):
        log_post(item, args.topic, getattr(args, "services", ""), out["link"])
    word = "ОТЛОЖЕН" if at else "ГОТОВО"
    tail = out["link"] or "ссылки нет, канал приватный"
    print("%s %s %s" % (cfg["marker"], word, item["id"]))
    print(tail)
    return 0


def neighbour_status(script, argv, timeout=20):
    """Спросить соседний скрипт одной строкой. Нет скрипта или упал: пустая строка."""
    path = os.path.join(HERE, script)
    if not os.path.exists(path):
        return ""
    try:
        done = subprocess.run([sys.executable, path] + list(argv), capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip()


def human_size(total):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if total < 1000 or unit == "ГБ":
            return "%d %s" % (total, unit) if unit == "Б" else "%.1f %s" % (total, unit)
        total /= 1000.0
    return "%d Б" % total


def human_ago(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d с назад" % seconds
    if seconds < 3600:
        return "%d мин назад" % (seconds // 60)
    if seconds < 86400:
        return "%d ч назад" % (seconds // 3600)
    return "%d дн назад" % (seconds // 86400)


def doctor_rows(cfg=None):
    """Строки доктора: (что, как, детали). Общие для tg.py doctor и tg_watch --doctor."""
    cfg = cfg or load_config()
    p = ensure_home()
    rows = []
    rows.append(("питон", "ок", "%d.%d.%d" % sys.version_info[:3]))
    writable = os.access(p["home"], os.W_OK)
    rows.append(("рабочая папка", "ок" if writable else "нет", p["home"]))
    rows.append(("настройки", "ок" if os.path.exists(p["config"]) else "по умолчанию",
                 "режим %s, канал %s, trust %s" % (cfg["mode"], cfg["channel"], cfg.get("trust"))))
    rows.append(("префикс", "ок", ", ".join(cfg["prefixes"])))
    inbox = read_inbox()
    st = load_state()
    fresh = [r for r in inbox if int(r.get("msg_id", 0)) > int(st.get("cursor", 0))]
    rows.append(("ящик", "ок", "строк %d, новых %d" % (len(inbox), len(fresh))))
    rows.append(("заявки", "ок", "всего %d, живых %d" % (len(st["items"]), len(active(st)))))
    rows.append(("линтер поста", "ок" if os.path.exists(os.path.join(HERE, "lint_post.py")) else "нет",
                 os.path.join("tools", "lint_post.py")))
    tele = telethon_ready()
    rows.append(("telethon", "ок" if tele else "нет",
                 "есть" if tele else "встанет сам: python3 tools/tg.py setup"))
    rows.append(("qrcode", "ок" if module_ready("qrcode") else "нет",
                 "QR в консоли" if module_ready("qrcode") else "вход покажет ссылку вместо QR"))
    info = creds()
    api = bool(info["api_id"]) and bool(info["api_hash"])
    rows.append(("ключи api", "ок" if api else "нет",
                 "api_id %s" % info["api_id"] if api else "нет в secrets/watch.env"))
    rows.append(("строка сессии", "ок" if info["session"] else "нет",
                 info["source"] or "вход одной командой: python3 tools/tg.py login"))
    rows.append(("канал", "ок" if cfg.get("channel") else "нет", str(cfg.get("channel") or "")))

    alive, lock = watcher_alive()
    if alive and lock:
        rows.append(("сторож", "ок", "жив, pid %d, сердцебиение %s"
                     % (lock["pid"], human_ago(time.time() - lock["heartbeat"]))))
    elif lock:
        rows.append(("сторож", "нет", "lock от pid %d устарел, сердцебиение %s"
                     % (lock["pid"], human_ago(time.time() - lock["heartbeat"]))))
    else:
        rows.append(("сторож", "нет", "не запущен: sh start-watch.sh или start-watch.ps1"))
    pending = outbox_pending()
    rows.append(("outbox", "ок" if not pending else "ждёт",
                 "пуст" if not pending else "%d строк ждут сторожа" % len(pending)))
    size, count = media_size()
    rows.append(("media", "ок", "%s в %d файлах" % (human_size(size), count) if count else "пусто"))
    log_path = p["log"]
    if os.path.exists(log_path):
        rows.append(("лог сторожа", "ок", "%s, %s" % (
            log_path, human_size(os.path.getsize(log_path)))))
    else:
        rows.append(("лог сторожа", "нет", log_path))
    auto = neighbour_status("autostart.py", ["status", "--short"])
    rows.append(("автозапуск", "ок" if auto.startswith("установлен") else "нет",
                 auto or "нет tools/autostart.py"))
    agent = neighbour_status("agent.py", ["which", "--short"])
    rows.append(("агент", "ок" if agent and not agent.startswith("нет") else "нет",
                 agent or "нет tools/agent.py"))
    return rows, info, tele


def cmd_doctor(args):
    rows, info, tele = doctor_rows()
    if args.as_json:
        print(json.dumps([{"что": a, "как": b, "детали": c} for a, b, c in rows],
                         ensure_ascii=False, indent=2))
    else:
        print("Мост в Телеграм, tg.py %s" % VERSION)
        print("")
        for a, b, c in rows:
            print("  %-16s %-4s %s" % (a, b, c))
        print("")
        if not tele or info["problems"]:
            print("Чего не хватает:")
            if not tele:
                print("  telethon: pip install telethon qrcode")
            for problem in info["problems"]:
                print("  %s" % problem)
            print("")
            print("Одной командой: python3 tools/tg.py setup")
        else:
            print("Всё на месте. Смена: python3 tools/tg_watch.py")
            print("Автозапуск после включения: python3 tools/autostart.py install")
    bad = [r for r in rows if r[1] == "нет" and r[0] in ("рабочая папка", "линтер поста")]
    return 1 if bad else 0


# ------------------------------------------------------------------- селфтест

def selftest():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    cfg = dict(DEFAULT_CONFIG)

    ok("текст без префикса не берём", decide_capture("купить хлеб", "", cfg) is None)
    ok("своё сообщение не берём", decide_capture("VIORA ЧЕРНОВИК r1", "", cfg) is None)
    ok("команда с префиксом берётся", decide_capture("/viora пуб", "", cfg) == "cmd")
    ok("русский префикс берётся", decide_capture("/виора сделай пост", "", cfg) == "cmd")
    ok("реплай на своё берётся", decide_capture("со второго раза", "VIORA ВОПРОС r1", cfg) == "answer")
    ok("реплай на чужое не берётся", decide_capture("ага", "привет", cfg) is None)
    ok("префикс без пробела не команда", decide_capture("/vioraпуб", "", cfg) is None)

    ok("пустая команда это помощь", classify("/viora", cfg)["kind"] == "help")
    ok("пуб это публикация", classify("/viora пуб", cfg)["kind"] == "publish")
    ok("публикуй с точкой это публикация", classify("/viora публикуй!", cfg)["kind"] == "publish")
    ok("что это статус", classify("/viora что", cfg)["kind"] == "status")
    ok("что с хвостом это заявка", classify("/viora что там по Notion", cfg)["kind"] == "request")
    ok("правь несёт хвост", classify("/viora правь первую строку", cfg)["arg"] == "первую строку")
    ok("отложи несёт время", classify("/viora отложи 20:30", cfg)["arg"] == "20:30")
    ok("стоп это отмена", classify("/viora стоп", cfg)["kind"] == "cancel")
    ok("сырьё это заявка", classify("/viora Notion даёт Business бесплатно", cfg)["kind"] == "request")
    ok("двоеточие после префикса", classify("/viora: сделай пост", cfg)["kind"] == "request")

    ok("виора без слеша это команда", classify("виора реши", cfg)["kind"] == "solve")
    ok("виора с запятой это команда", classify("Виора, реши", cfg)["kind"] == "solve")
    ok("виоралогия не команда", classify("виоралогия сегодня", cfg)["kind"] == "skip")
    ok("одна виора это помощь", classify("виора", cfg)["kind"] == "help")
    ok("реши несёт хвост",
       classify("/viora реши задачу про углы", cfg)["arg"] == "задачу про углы")
    ok("что тут это решение", classify("/viora что тут", cfg)["kind"] == "solve")
    ok("помоги это решение", classify("/viora помоги с интегралом", cfg)["kind"] == "solve")
    ok("решительный шаг это заявка", classify("/viora решительный шаг", cfg)["kind"] == "request")
    ok("помощь осталась карточкой", classify("/viora помощь", cfg)["kind"] == "help")
    ok("пк это состояние компа", classify("/viora пк", cfg)["kind"] == "pc")
    ok("статус компа фразой", classify("/viora статус компа", cfg)["kind"] == "pc")
    ok("место с хвостом это заявка",
       classify("/viora место в облаке кончилось", cfg)["kind"] == "request")
    ok("погода это погода", classify("/viora погода", cfg)["kind"] == "weather")
    ok("погода несёт город", classify("/viora погода Казань", cfg)["arg"] == "Казань")
    ok("погодные аномалии это заявка", classify("/viora погодные аномалии", cfg)["kind"] == "request")
    ok("утро это дайджест", classify("/viora утро", cfg)["kind"] == "morning")
    ok("дайджест это дайджест", classify("/viora дайджест", cfg)["kind"] == "morning")
    ok("утро с хвостом это заявка", classify("/viora утро в деревне", cfg)["kind"] == "request")
    ok("напомни несёт хвост",
       classify("/viora напомни выпить таблетку", cfg)["kind"] == "remind")
    ok("каждое утро это напоминание",
       classify("/viora каждое утро дайджест", cfg)["kind"] == "remind")
    ok("напоминание несёт текст",
       classify("/viora напомни 09:30 таблетка", cfg)["arg"] == "09:30 таблетка")
    ok("напомни без хвоста не напоминание", classify("/viora напомни", cfg)["kind"] != "remind")
    ok("у заявки есть пустое медиа", classify("/viora тема", cfg)["media"] == [])
    ok("медиа доезжает до заявки",
       classify("/viora реши", cfg, "cmd", ["/tmp/a.jpg"])["media"] == ["/tmp/a.jpg"])
    ok("решение бывает без хвоста", classify("/viora реши", cfg)["kind"] == "solve")
    ok("ответ автора несёт медиа",
       classify("вот фото", cfg, "answer", ["/tmp/b.jpg"])["media"] == ["/tmp/b.jpg"])
    for extra_kind in ("solve", "pc", "weather", "remind", "morning"):
        ok("у вида %s есть подсказка" % extra_kind, bool(HINTS.get(extra_kind)))
    ok("карточка знает про решение", "реши задачу" in card_text(cfg))
    ok("карточка знает про обращение без слеша", "виора реши" in card_text(cfg))
    ok("перенос строки в заявке", "\n" in classify("/viora тема\nвторая строка", cfg)["arg"])

    rows = parse_mcp("ID: 11 | Me | Date: 2026-08-25 18:00:00+00:00 | Message: /viora пуб\n"
                     "ID: 12 | Me | Date: 2026-08-25 18:01:00+00:00 | Message: /viora тема\\nвторая строка\n"
                     "мусорная строка\n")
    ok("разбор вывода MCP: две строки", len(rows) == 2)
    ok("разбор вывода MCP: перенос", rows[1]["text"].endswith("вторая строка"))
    ok("разбор вывода MCP: id", rows[0]["msg_id"] == 11)

    ok("окно публикации по умолчанию", window_bounds(cfg) == ((16, 30), (21, 0)))
    tz = tzinfo_of(cfg)
    early = datetime.datetime(2026, 8, 25, 9, 0, tzinfo=tz)
    late = datetime.datetime(2026, 8, 25, 23, 0, tzinfo=tz)
    inside = datetime.datetime(2026, 8, 25, 18, 0, tzinfo=tz)
    ok("утром ждём окно", next_slot(cfg, None, early).hour == 16)
    ok("ночью уходим на завтра", next_slot(cfg, None, late).day == 26)
    ok("внутри окна публикуем скоро", next_slot(cfg, None, inside).hour == 18)
    ok("точное время в будущем", next_slot(cfg, "20:30", inside).hour == 20)

    # Полный проход по сценарию в отдельной папке, чтобы не тронуть настоящую.
    tmp = tempfile.mkdtemp(prefix="viora-tg-")
    old = os.environ.get("VIORA_TG_HOME")
    os.environ["VIORA_TG_HOME"] = tmp
    try:
        save_config(dict(DEFAULT_CONFIG))
        added = append_inbox(feed_rows(
            "ID: 20 | Me | Date: d | Message: /viora пост про пак абузов\n"
            "ID: 21 | Me | Date: d | Message: VIORA ЧЕРНОВИК r1 эхо своего же сообщения\n"))
        ok("в ящик легла только заявка", added == 1)
        ok("повтор не дублируется", append_inbox(feed_rows("ID: 20 | Me | Date: d | Message: /viora пост\n")) == 0)

        st = load_state()
        got = take_next(st, load_config())
        ok("next отдал заявку", got and got["kind"] == "request" and got["id"] == "r1")
        ok("курсор сдвинулся", load_state()["cursor"] == 20)
        ok("второй раз заявку не отдаёт", take_next(load_state(), load_config()) is None)

        sample = os.path.join(HERE, "sample-post.txt")
        args = argparse.Namespace(req="r1", file=sample, rubric="", media=False)
        code = cmd_stage(args)
        ok("stage пропустил чистый пост", code == 0)
        ok("черновик сохранён", os.path.exists(os.path.join(tmp, "drafts", "r1.txt")))

        cmd_sent(argparse.Namespace(req="r1", msg_id=99))
        ok("статус ждёт добро", find(load_state(), "r1")["status"] == "await")

        append_inbox([{"msg_id": 30, "text": "/viora пуб", "src": "watch", "kind": "cmd"}])
        got = take_next(load_state(), load_config())
        ok("пуб нашёл свою заявку", got and got["kind"] == "publish" and got["id"] == "r1")

        with open(sample, encoding="utf-8") as handle:
            want = handle.read().strip("\n")
        out = os.path.join(tmp, "payload.txt")
        cmd_payload(argparse.Namespace(req="r1", out=out))
        with open(out, encoding="utf-8") as handle:
            got_text = handle.read().strip("\n")
        ok("payload отдаёт пост знак в знак", got_text == want)
        ok("в payload нет служебной шапки", not got_text.startswith("VIORA"))

        cmd_published(argparse.Namespace(req="r1", msg_id=612, link="https://t.me/VioraStudio/612",
                                        scheduled=False, topic="", services=""))
        ok("заявка закрыта", find(load_state(), "r1")["status"] == "published")
        ok("живых заявок нет", not active(load_state()))
        ok("статус упоминает ссылку", "612" in status_text())

        append_inbox([{"msg_id": 40, "text": "/viora пуб", "src": "watch", "kind": "cmd"}])
        got = take_next(load_state(), load_config())
        ok("пуб без черновика не находит цель", got and got["kind"] == "publish" and not got["id"])

        append_inbox([{"msg_id": 41, "text": "/viora пост по скринам", "src": "watch", "kind": "cmd",
                       "media": ["/tmp/m/41/a.jpg", "/tmp/m/41/b.jpg"]}])
        got = take_next(load_state(), load_config())
        ok("next отдаёт пути к вложениям", got and got.get("media") == ["/tmp/m/41/a.jpg", "/tmp/m/41/b.jpg"])
        append_inbox([{"msg_id": 42, "text": "/viora правь короче", "src": "watch", "kind": "cmd"}])
        got = take_next(load_state(), load_config())
        ok("у команды без файлов media пустой список", got and got.get("media") == [])

        ok("карточка помощи начинается с метки", card_text().startswith("VIORA ПОМОЩЬ"))
        ok("карточка не уйдёт в ящик", decide_capture(card_text(), "", load_config()) is None)

        # проверка связи: короткое слово это ответ сторожа, а не заявка на пост
        ok("как дела это проверка связи",
           classify("/viora как дела?", load_config())["kind"] == "ping")
        ok("привет это проверка связи",
           classify("/виора привет", load_config())["kind"] == "ping")
        ok("длинная строка остаётся заявкой",
           classify("/viora как дела с абузами на курсор", load_config())["kind"] == "request")
        ok("ответ на связь в одну строку", "\n" not in ping_text())

        # ключи и строка сессии берутся из файла, а не только из окружения
        keep = {}
        for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "VIORA_SECRETS") + SESSION_ENVS:
            keep[key] = os.environ.pop(key, None)
        secret_dir = os.path.join(tmp, "secrets")
        os.environ["VIORA_SECRETS"] = secret_dir
        write_atomic(os.path.join(secret_dir, "watch.env"),
                     "# заметка\nTELEGRAM_API_ID=123456\nTELEGRAM_API_HASH=hash\n")
        info = creds()
        ok("ключи читаются из secrets",
           info["api_id"] == "123456" and info["api_hash"] == "hash")
        ok("без сессии просят login", any("login" in row for row in info["problems"]))
        path = save_session("STRING-FOR-TEST")
        ok("сессия легла в тот же файл", os.path.dirname(path) == secret_dir)
        again = creds()
        ok("сессия читается обратно", again["session"] == "STRING-FOR-TEST")
        ok("ключи не потерялись", again["api_id"] == "123456")
        ok("заметка не стала ключом", "# заметка" not in read_env_file(path))
        ok("ссылка на пост канала",
           link_for("@VioraStudio", 612) == "https://t.me/VioraStudio/612")
        ok("для Избранного ссылки нет", link_for("me", 5) == "")
        ok("канал берётся из настроек",
           target_of("channel", load_config()) == "@VioraStudio")
        ok("Избранное остаётся me", target_of("Избранное", load_config()) == "me")
        for key, value in keep.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        # Своё против чужого: метка капсом плюс капсовое слово, а обращение автора нет
        cfg_now = load_config()
        ok("viora реши это команда, а не своё", decide_capture("viora реши", "", cfg_now) == "cmd")
        ok("Viora, погода это команда", decide_capture("Viora, погода", "", cfg_now) == "cmd")
        ok("VIORA НА СМЕНЕ это своё", decide_capture(ping_text(cfg_now), "", cfg_now) is None)
        ok("VIORA ВОПРОС это своё", decide_capture("VIORA ВОПРОС r1\nтекст", "", cfg_now) is None)
        ok("одна метка это своё", starts_with_marker("VIORA", cfg_now))
        ok("метка со строчным словом не своё", not starts_with_marker("VIORA реши", cfg_now))

        # Миграция префиксов: старый файл получает обращение без слеша, новый нет
        write_atomic(paths()["config"], json.dumps({"prefixes": ["/viora"]}) + "\n")
        ok("старый конфиг получил виору без слеша", "виора" in load_config()["prefixes"])
        cfg_new = load_config()
        cfg_new["prefixes"] = ["/viora"]
        save_config(cfg_new)
        ok("после save_config список автора не трогаем", load_config()["prefixes"] == ["/viora"])
        ok("trust по умолчанию read", load_config()["trust"] == "read")
        save_config(dict(DEFAULT_CONFIG))

        # Один процесс в сети: lock, сердцебиение, outbox и подтверждение
        ok("без lock сторож мёртв", watcher_alive() == (False, None))
        write_lock(pid=os.getpid())
        alive, row = watcher_alive()
        ok("свежий lock значит жив", alive and row["pid"] == os.getpid())
        write_lock(pid=os.getpid(), extra={"heartbeat": time.time() - 999})
        alive, row = watcher_alive()
        ok("старое сердцебиение, но живой pid: ещё жив", alive)
        write_lock(pid=999999, extra={"heartbeat": time.time() - 999})
        alive, row = watcher_alive()
        ok("старое сердцебиение и чужой pid: мёртв", not alive and row["pid"] == 999999)
        ok("чужой lock не снимаем", not clear_lock(pid=os.getpid()))
        ok("свой lock снимаем", clear_lock(pid=999999) and read_lock() is None)

        row = outbox_put("текст", "me", 5, None, "r1", "draft")
        ok("записка легла в outbox", outbox_pending()[0]["id"] == row["id"])
        ok("без подтверждения ждём и уходим по таймауту",
           outbox_wait(row["id"], timeout=1, sleeper=lambda s: None) is None)
        outbox_done(row, msg_id=77)
        got = outbox_wait(row["id"], timeout=1, sleeper=lambda s: None)
        ok("подтверждение нашлось с msg_id", got and got["msg_id"] == 77 and got["req"] == "r1")
        ok("после done записка не ждёт", outbox_pending() == [])

        # send_text при живом стороже идёт через outbox, без сети
        write_lock(pid=os.getpid())
        keep_sleeper = SLEEPER

        def fake_confirm(seconds):
            for pend in outbox_pending():
                outbox_done(pend, msg_id=4821, link="")

        try:
            globals()["SLEEPER"] = fake_confirm
            pend_before = len(outbox_pending())
            out = send_text("VIORA ЧЕРНОВИК r1\nтекст", "me", 0, None, load_config(),
                            req="r1", role="draft")
            ok("send_text при живом стороже идёт через outbox",
               out["msg_id"] == 4821 and out["via"] == "watch")
            ok("записка подтверждена и не висит", len(outbox_pending()) == pend_before)
            done_rows = read_jsonl(paths()["outbox_done"])
            ok("в done записаны заявка и роль",
               done_rows[-1]["req"] == "r1" and done_rows[-1]["role"] == "draft")
        finally:
            globals()["SLEEPER"] = keep_sleeper
        clear_lock()

        # Чистка media: старая папка уходит, свежая остаётся
        old_dir = os.path.join(paths()["media"], "1")
        new_dir = os.path.join(paths()["media"], "2")
        os.makedirs(old_dir)
        os.makedirs(new_dir)
        write_atomic(os.path.join(old_dir, "a.jpg"), "x")
        write_atomic(os.path.join(new_dir, "b.jpg"), "yy")
        stale = time.time() - 20 * 86400
        os.utime(old_dir, (stale, stale))
        ok("media считает размер", media_size() == (3, 2))
        ok("старая папка media удалена", clean_media(days=14) == 1 and not os.path.exists(old_dir))
        ok("свежая папка media на месте", os.path.exists(new_dir))

        # QR-вход без сети: код печатается, по таймауту пересоздаётся, двухфакторка спрашивается
        class TimeoutErr(Exception):
            pass

        class SessionPasswordNeededError(Exception):
            pass

        class FakeLogin(object):
            def __init__(self):
                self.recreated = 0
                self.url = "tg://login?token=abc"

            def recreate(self):
                self.recreated += 1
                self.url = "tg://login?token=def"

        class FakeClient(object):
            def __init__(self, script):
                self.script = list(script)
                self.login = FakeLogin()
                self.password = None

            def qr_login(self):
                return self.login

            def sign_in(self, password=None):
                self.password = password
                return "user-after-2fa"

        printed = []

        def fake_printer(url):
            printed.append(url)
            return ["[QR %s]" % url]

        def waiter_for(client):
            def wait(login):
                step = client.script.pop(0)
                if isinstance(step, Exception):
                    raise step
                return step
            return wait

        import contextlib
        import io as _io
        buf = _io.StringIO()
        fake = FakeClient([TimeoutErr("timeout"), "user"])
        with contextlib.redirect_stdout(buf):
            got = login_qr(fake, printer=fake_printer, waiter=waiter_for(fake), tries=3)
        ok("QR: после таймаута код пересоздан и вход состоялся", got == "user" and fake.login.recreated == 1)
        ok("QR: код напечатан дважды", len(printed) == 2 and printed[-1].endswith("def"))
        ok("QR: ссылка tg://login печатается текстом", "tg://login?token=" in buf.getvalue())
        ok("QR: подсказка про Устройства", "Подключить устройство" in buf.getvalue())

        keep_prompt = PASSWORD_PROMPT
        globals()["PASSWORD_PROMPT"] = lambda: "cloud-pass"
        try:
            fake = FakeClient([SessionPasswordNeededError()])
            with contextlib.redirect_stdout(_io.StringIO()):
                got = login_qr(fake, printer=fake_printer, waiter=waiter_for(fake), tries=2)
            ok("QR: двухфакторка спрашивает пароль облака", got == "user-after-2fa" and fake.password == "cloud-pass")
            globals()["PASSWORD_PROMPT"] = lambda: ""
            fake = FakeClient([SessionPasswordNeededError()])
            failed = False
            try:
                with contextlib.redirect_stdout(_io.StringIO()):
                    login_qr(fake, printer=fake_printer, waiter=waiter_for(fake), tries=2)
            except NetProblem:
                failed = True
            ok("QR: без пароля облака честный отказ", failed)
        finally:
            globals()["PASSWORD_PROMPT"] = keep_prompt
        fake = FakeClient([TimeoutErr("t"), TimeoutErr("t")])
        failed = False
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                login_qr(fake, printer=fake_printer, waiter=waiter_for(fake), tries=2)
        except NetProblem:
            failed = True
        ok("QR: лимит попыток даёт понятный отказ", failed)
        ok("QR без qrcode не падает", isinstance(qr_lines("tg://login?token=x"), list))
    finally:
        if old is None:
            os.environ.pop("VIORA_TG_HOME", None)
        else:
            os.environ["VIORA_TG_HOME"] = old
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [name for name, good in checks if not good]
    for name, good in checks:
        print("%s %s" % ("PASS" if good else "FAIL", name))
    print("Итого: %d проверок моста, провалилось %d" % (len(checks), len(bad)))
    return 1 if bad else 0


# ----------------------------------------------------------------------- разбор

def build_parser():
    ap = argparse.ArgumentParser(description="Мост между Избранным и скиллом VIORA STUDIO")
    ap.add_argument("--version", action="version", version="tg.py %s" % VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("doctor", help="проверить окружение моста")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("card", help="карточка команд для автора")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("next", help="следующая команда автора")
    p.add_argument("--wait", type=int, default=0, help="сколько секунд ждать, потолок %d" % MAX_WAIT)
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("feed", help="положить вывод list_messages в ящик")
    p.add_argument("--file", help="файл с выводом, по умолчанию stdin")
    p.add_argument("--stdin", action="store_true", help="читать stdin")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("stage", help="проверить черновик и получить текст для Избранного")
    p.add_argument("req")
    p.add_argument("--file", required=True)
    p.add_argument("--rubric", default="")
    p.add_argument("--media", action="store_true")
    p.add_argument("--send", action="store_true", help="сразу отправить автору в Избранное")
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("sent", help="отметить, что черновик ушёл автору")
    p.add_argument("req")
    p.add_argument("--msg-id", dest="msg_id", required=True)
    p.set_defaults(func=cmd_sent)

    p = sub.add_parser("payload", help="текст поста для канала, знак в знак")
    p.add_argument("req")
    p.add_argument("--out")
    p.set_defaults(func=cmd_payload)

    p = sub.add_parser("ask", help="текст вопроса автору")
    p.add_argument("req")
    p.add_argument("--text", required=True)
    p.add_argument("--send", action="store_true", help="сразу отправить вопрос автору")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("asked", help="отметить id отправленного вопроса")
    p.add_argument("req")
    p.add_argument("--msg-id", dest="msg_id", required=True)
    p.set_defaults(func=cmd_asked)

    p = sub.add_parser("published", help="отметить публикацию")
    p.add_argument("req")
    p.add_argument("--msg-id", dest="msg_id", default="0")
    p.add_argument("--link", default="")
    p.add_argument("--scheduled", action="store_true")
    p.add_argument("--topic", default="", help="тема для лога постов")
    p.add_argument("--services", default="")
    p.set_defaults(func=cmd_published)

    p = sub.add_parser("cancel", help="отменить заявку")
    p.add_argument("req")
    p.add_argument("--why", default="")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("status", help="что в работе")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("when", help="время следующей публикации в окне канала")
    p.add_argument("--at", help="точное время вида 20:30")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_when)

    p = sub.add_parser("config", help="посмотреть и поменять настройки")
    p.add_argument("--set", action="append", help="ключ=значение")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("setup", help="первый запуск: поставить telethon и qrcode, войти")
    p.add_argument("--phone", action="store_true", help="вход по номеру, а не по QR")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("login", help="вход в Телеграм: QR с телефона, --phone для номера")
    p.add_argument("--force", action="store_true", help="перелогин, даже если сессия есть")
    p.add_argument("--phone", action="store_true", help="старый путь: номер и код")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("send", help="отправить текст себе или в канал")
    p.add_argument("--text", default="")
    p.add_argument("--file")
    p.add_argument("--to", default="me", help="me, channel или @имя")
    p.add_argument("--reply-to", dest="reply_to", type=int, default=0)
    p.add_argument("--at", help="отложить на время вида 20:30")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--direct", action="store_true", help="своим подключением, мимо сторожа")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("ping", help="ответить автору, что смена идёт")
    p.add_argument("--reply-to", dest="reply_to", type=int, default=0)
    p.add_argument("--local", action="store_true", help="только напечатать")
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("poll", help="забрать заявки из Избранного без сторожа")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("publish", help="отправить черновик в канал")
    p.add_argument("req")
    p.add_argument("--to", default="channel")
    p.add_argument("--at", help="время вида 20:30, иначе сразу")
    p.add_argument("--topic", default="")
    p.add_argument("--services", default="")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("selftest", help="свои тесты")
    p.set_defaults(func=lambda a: selftest())
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        return selftest()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
