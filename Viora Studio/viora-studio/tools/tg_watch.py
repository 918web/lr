#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg_watch.py 1.1.0

Сторож Избранного. Единственный процесс, который держит соединение с Телеграмом.

Что делает:
    1. Слушает только чат с самим собой и берёт только адресованное агенту:
       сообщения с префиксом и ответы реплаем на свои сообщения. Остальное личное.
    2. Скачивает фото и документы до 20 МБ из пойманного сообщения в media/<msg_id>/,
       альбом собирает в одну заявку.
    3. Раз в секунду разбирает tg-outbox.jsonl: отправляет записки от tg.py send,
       stage --send, ask --send, publish и ping, пишет подтверждение с msg_id
       в tg-outbox-done.jsonl и сам отмечает заявку через sent, asked, published.
    4. Мозг v2: pc, weather, morning отвечает сам через life.py, remind через
       schedule.py, solve и свободный текст через agent.py (подписочный CLI),
       потом ai.py по ключу, потом каркас без модели. Пока думает, пишет «взял, думаю».
    5. Планировщик: раз в 60 секунд зовёт schedule.py due и выполняет задачи.
       Пропущенные за время сна выполняются один раз, если прошло меньше 3 часов.
    6. Lock-файл с pid и сердцебиением: второй экземпляр не стартует и говорит почему.
       Реконнект с растущей паузой, лог в watch.log с ротацией на 1 МБ.

Запуск:
    python3 tools/tg_watch.py                 держать смену
    python3 tools/tg_watch.py --once --timeout 600
    python3 tools/tg_watch.py --quiet         без вывода в консоль, только лог
    python3 tools/tg_watch.py --doctor
    python3 tools/tg_watch.py --selftest

Ключи и строка сессии лежат в secrets/watch.env, читает их сам мост.
Вход одной командой: python3 tools/tg.py login

Коды возврата: 0 порядок, 1 проблема, 3 второй экземпляр при живом первом.
"""

import argparse
import asyncio
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

VERSION = "1.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tg as TG  # noqa: E402

HEARTBEAT_EVERY = 5
OUTBOX_EVERY = 1.0
SCHEDULE_EVERY = 60
CATCHUP_HOURS = 3
ALBUM_WAIT = 1.5
MEDIA_LIMIT = 20 * 1024 * 1024
MEDIA_DAYS = 14
LOG_LIMIT = 1024 * 1024
RECONNECT_MIN = 5
RECONNECT_MAX = 300
TOOL_TIMEOUT = 300
AGENT_TIMEOUT = 600


# ----------------------------------------------------------------------- лог

QUIET = False


def rotate_log(path, limit=LOG_LIMIT):
    """Один запасной файл: watch.log.1. Больше истории сторожу не нужно."""
    try:
        if os.path.getsize(path) < limit:
            return False
    except OSError:
        return False
    backup = path + ".1"
    try:
        if os.path.exists(backup):
            os.unlink(backup)
        os.replace(path, backup)
    except OSError:
        return False
    return True


def log(text):
    """Строка в watch.log и в консоль, если её не выключили."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "%s %s" % (stamp, text)
    try:
        path = TG.paths()["log"]
        TG.ensure_home()
        rotate_log(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    if not QUIET:
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("utf-8", "replace").decode("ascii", "replace"))


# --------------------------------------------------------------------- доступы

def creds():
    """Доступы берём у моста: он читает и окружение, и secrets/watch.env."""
    info = TG.creds()
    return {"api_id": info["api_id"], "api_hash": info["api_hash"],
            "session": info["session"], "session_env": info["source"],
            "problems": list(info["problems"]), "warn": ""}


def quick_answer(text, cfg, verdict):
    """Что сторож отвечает сам без всякой модели: связь, помощь, статус."""
    if verdict != "cmd":
        return ""
    cmd = TG.classify(text, cfg, verdict)
    kind = cmd.get("kind")
    if kind == "ping":
        return TG.ping_text(cfg)
    if kind == "help":
        return TG.card_text(cfg)
    if kind == "status":
        return TG.status_text(cfg)
    return ""


# ----------------------------------------------------------------------- мозг

# По этим словам видно, что человеку нужен текст в канал, а не разговор.
POST_WORDS = ("пост", "напиши", "разбор", "гайд", "абуз", "подборк", "сервис",
              "шортс", "девлог", "релиз", "опрос", "лидмагнит", "неробит",
              "черновик", "текст в канал", "халяв", "бесплатн")
# А по этим видно разговор. Разговор сильнее: лучше ответить словами,
# чем выдать пост там, где его не просили.
TALK_WORDS = ("как дела", "что думаешь", "объясни", "почему", "зачем",
              "как лучше", "посоветуй", "что скажешь", "идеи", "придумай тем",
              "стоит ли", "что такое", "в чем разница", "в чём разница")

THINKING = "взял, думаю"


def brain_on(cfg):
    """Мозг работает, пока его явно не выключили."""
    value = cfg.get("brain")
    if value is None:
        value = os.environ.get("VIORA_BRAIN", "on")
    return str(value).strip().lower() not in ("off", "0", "no", "нет", "выкл")


def wants_post(text):
    """Заявка на пост или обычный вопрос."""
    low = " ".join((text or "").lower().split())
    if not low:
        return False
    if any(word in low for word in TALK_WORDS):
        return False
    if low.startswith("http") or " http" in low:
        return True
    return any(word in low for word in POST_WORDS)


def run_tool(script, args, timeout=TOOL_TIMEOUT):
    """Запустить скрипт скилла и вернуть код и вывод. Наружу не падаем."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        done = subprocess.run(
            [sys.executable, os.path.join(HERE, script)] + [str(a) for a in args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout,
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 1, "не успел за %d секунд, скажи короче или повтори" % timeout
    except OSError as err:
        return 1, "не запустился: %s" % err
    out = (done.stdout or b"").decode("utf-8", "replace").strip()
    err = (done.stderr or b"").decode("utf-8", "replace").strip()
    return done.returncode, out or err


# Тесты подменяют эту точку, а не сам subprocess: так видно, кого звали.
RUNNER = run_tool


def fit(text, limit=None):
    """В Телеграме есть потолок сообщения, и ломаться о него глупо."""
    limit = int(limit or TG.TG_LIMIT)
    body = text or ""
    if len(body) <= limit:
        return body
    return body[:limit - 60].rstrip() + "\n\nдальше обрезал, чтобы влезло в сообщение"


HANDOFF = "\n".join([
    "Текст добери агентом в редакторе: правила он читает сам из SKILL.md.",
    "Заявка уже в ящике: python3 tools/tg.py next --json",
])

NO_BRAIN = "\n".join([
    "Ни CLI агента, ни ключа модели на этой машине нет, и это нормально.",
    "Заявку записал, забери её командой: python3 tools/tg.py next --json",
    "Хочешь ответы прямо в телеге: поставь claude, codex, agy или gemini,",
    "или положи один ключ в secrets/ai.env",
])


def local_alive(base, timeout=0.5):
    """Слушает ли кто-то адрес локальной модели. Без сети, один connect."""
    try:
        parts = urllib.parse.urlsplit(base or "")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ai_ready():
    """Есть ли ключ модели. Без ключа работаем на агента, а не падаем.

    ai.py считает ollama и lmstudio готовыми всегда, ключ им не нужен. Сторожу
    этого мало: если на порту никого нет, автору честнее сказать «модели нет»,
    чем «модель не ответила».
    """
    try:
        import ai as AI
        conf = AI.settings()
    except Exception:
        return False
    if not conf.get("ready"):
        return False
    if conf.get("key"):
        return True
    return LOCAL_ALIVE(conf.get("base") or "")


def agent_ready():
    """Есть ли подписочный CLI агента в PATH."""
    try:
        import agent as AG
        return bool(AG.find_clis())
    except Exception:
        return False


# Тесты подменяют эти точки: так проверяются все три ступени мозга.
LOCAL_ALIVE = local_alive
READY = ai_ready
AGENT_READY = agent_ready


def agent_answer(text, kind, media, cfg):
    """Ступень один: agent.py через файл задачи. Пусто, если агент не ответил."""
    task = {"text": text, "media": list(media or []), "kind": kind,
            "cwd": os.path.dirname(TG.SKILL)}
    fd, path = tempfile.mkstemp(prefix="viora-task-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(task, handle, ensure_ascii=False)
    try:
        code, out = RUNNER("agent.py", ["run", "--task", path, "--json"], timeout=AGENT_TIMEOUT)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not out:
        return "", "агент промолчал"
    try:
        data = json.loads(out[out.find("{"):])
    except ValueError:
        return "", out[:200]
    if data.get("ok") and data.get("text"):
        return data["text"], ""
    return "", data.get("why") or "агент не ответил"


def post_from_json(out):
    """Из вывода post.py --json вытащить пост, бриф, скелет и рубрику. Битый JSON даёт пустоту."""
    try:
        data = json.loads(out[out.find("{"):])
    except (ValueError, TypeError):
        return "", "", "", ""
    if not isinstance(data, dict):
        return "", "", "", ""
    return (data.get("post") or "").strip(), (data.get("brief") or "").strip(), \
        (data.get("skeleton") or "").strip(), (data.get("rubric") or "").strip()


def gate_draft(text, rubric="", media=False):
    """Тот же гейт, что у stage: lint_post.py --fix, потом --strict.

    Черновик от мозга публикует сам сторож по слову «пуб», поэтому в канал
    он не может уйти мимо линтера. Возвращает (текст после правки, список ошибок).
    Механику чинит --fix, смысловые ошибки отдаём назад агенту на второй заход.
    """
    body = (text or "").strip("\n")
    if not body.strip():
        return "", ["пустой пост"]
    fd, path = tempfile.mkstemp(prefix="viora-draft-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body + "\n")
    try:
        argv = [path, "--fix"]
        if rubric:
            argv += ["--rubric", rubric]
        if media:
            argv.append("--media")
        RUNNER("lint_post.py", argv, timeout=60)
        with open(path, encoding="utf-8") as handle:
            fixed = handle.read().strip("\n")
        argv = [path, "--strict", "--json"]
        if rubric:
            argv += ["--rubric", rubric]
        if media:
            argv.append("--media")
        code, out = RUNNER("lint_post.py", argv, timeout=60)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if code == 0:
        return fixed, []
    try:
        report = json.loads(out[out.find("{"):])
    except (ValueError, TypeError):
        report = {}
    rows = []
    for item in (report.get("errors") or []) + (report.get("warns") or []):
        if isinstance(item, dict):
            rows.append(("%s %s" % (item.get("code") or "", item.get("message") or "")).strip())
    return fixed, rows or [out.strip()[:300] or "линтер не пустил"]


def guess_rubric(post_text, request_text):
    """Рубрика для гейта: хэштег из текста агента, иначе угадываем по заявке."""
    try:
        import lint_post as LP
        found = LP.detect_rubric(post_text or "")
        if found:
            return found
    except Exception:
        pass
    try:
        import post as P
        return P.pick_rubric(request_text or "")
    except Exception:
        return ""


def brain_post(text, cfg, media, item):
    """Пост от agent.py, доведённый через гейт линтера. Пусто, если не вышло.

    Первый заход: агент пишет по правилам скилла. Линтер забраковал: второй заход
    с его претензиями в промпте. И после второго не чисто: пост автору не показываем,
    заявка остаётся в ящике для агента в редакторе.
    """
    rubric = (item or {}).get("rubric") or cfg.get("rubric_default", "")
    ask = text
    for attempt in range(2):
        answer, why = agent_answer(ask, "request", media, cfg)
        if not answer:
            log("agent.py не ответил: %s" % why)
            return "", why
        if not rubric:
            rubric = guess_rubric(answer, text)
            if item is not None:
                item["rubric"] = rubric
        # Вложения к заявке это сырьё для поста, в канал сторож шлёт текст,
        # поэтому гейт идёт без --media и потолок у поста обычный.
        fixed, problems = gate_draft(answer, rubric)
        if not problems:
            return fixed, ""
        log("черновик мозга не прошёл линтер (%d): %s" % (attempt + 1, "; ".join(problems[:4])))
        ask = "%s\n\nПрошлый вариант линтер забраковал, исправь именно это:\n%s\n\nПрошлый вариант:\n%s" % (
            text, "\n".join("- " + row for row in problems[:8]), fixed)
    return "", "линтер не пустил черновик: " + "; ".join(problems[:3])


def brain_reply(text, cfg, kind="", media=None, item=None):
    """Ответ по существу: пост, решение или разговор.

    Порядок ступеней: agent.py (подписочный CLI, ключи не нужны), ai.py по ключу,
    каркас без модели. Ответ всегда начинается с маркера капсом: свои сообщения
    сторож в ящик не кладёт, иначе услышит сам себя и уйдёт в круг.

    Для kind=request с заявкой item ответ собран как карточка stage: шапка,
    факты, подсказка «пуб» и «правь», пустая строка, тело поста. Тело после пустой
    строки ложится в черновик заявки, и «виора пуб» публикует его знак в знак.
    Пост от мозга идёт через тот же гейт линтера, что и stage у агента.
    """
    body = (text or "").strip()
    media = list(media or [])
    if not body and not media:
        return ""
    marker = str(cfg.get("marker") or "VIORA")
    if not kind:
        kind = "request" if wants_post(body) else "talk"
    answer = ""
    if kind == "request":
        gate_note = ""
        if AGENT_READY():
            answer, gate_note = brain_post(body, cfg, media, item)
        smart = READY()
        if not answer:
            code, out = RUNNER("post.py", [body, "--json"] if smart else [body, "--no-ai"])
            if not out:
                return ""
            if not smart:
                head = "КАРКАС, черновик агента не прошёл линтер" if gate_note else "КАРКАС, модели нет"
                return "%s %s\n%s\n\n%s" % (marker, head, out, HANDOFF)
            answer, brief, skeleton, picked = post_from_json(out)
            if not answer:
                return "%s КАРКАС, модель не собрала пост\n%s\n\n%s\n\n%s" % (
                    marker, brief, skeleton, HANDOFF)
            rubric = (item or {}).get("rubric") or cfg.get("rubric_default", "") or picked
            if item is not None and not item.get("rubric"):
                item["rubric"] = rubric
            answer, problems = gate_draft(answer, rubric)
            if problems:
                log("пост от ai.py не прошёл линтер: %s" % "; ".join(problems[:4]))
                return "%s КАРКАС, пост модели не прошёл линтер\n%s\n\n%s\n\n%s" % (
                    marker, "\n".join("- " + row for row in problems[:6]), skeleton, HANDOFF)
        if item is not None:
            return TG.draft_message(item, cfg, answer)
        return "%s ЧЕРНОВИК\n\n%s" % (marker, answer)
    if AGENT_READY():
        answer, why = agent_answer(body, kind, media, cfg)
        if not answer:
            log("agent.py не ответил: %s" % why)
    if answer:
        head = "РЕШЕНИЕ" if kind == "solve" else "ОТВЕТ"
        return "%s %s\n%s" % (marker, head, answer)
    if not READY():
        if media:
            return "%s БЕЗ МОДЕЛИ\nКартинку разобрать некому.\n%s" % (marker, NO_BRAIN)
        return "%s БЕЗ МОДЕЛИ\n%s" % (marker, NO_BRAIN)
    code, out = RUNNER("ai.py", ["ask", body or "опиши, что на картинке, по путям: %s" % ", ".join(media)])
    if not out:
        return ""
    if code:
        # Сырую ругань транспорта в телегу не тащим: толку ноль, шума много.
        return "%s НЕ ВЫШЛО\nМодель не ответила. Проверка: python3 tools/ai.py --doctor\n%s" % (
            marker, HANDOFF)
    head = "РЕШЕНИЕ" if kind == "solve" else "ОТВЕТ"
    return "%s %s\n%s" % (marker, head, out)


def card_body(answer):
    """Тело карточки после первой пустой строки: то, что уйдёт в канал."""
    cut = (answer or "").find("\n\n")
    return answer[cut + 2:].strip("\n") if cut != -1 else ""


def life_reply(kind, arg, cfg):
    """pc, weather, morning: сторож отвечает сам через life.py, мгновенно и без модели."""
    marker = str(cfg.get("marker") or "VIORA")
    argv = [kind]
    if kind == "weather" and arg:
        argv.append(arg)
    code, out = RUNNER("life.py", argv, timeout=60)
    head = {"pc": "ПК", "weather": "ПОГОДА", "morning": "УТРО"}.get(kind, kind.upper())
    if not out:
        return "%s %s\nданных нет: python3 tools/life.py %s ничего не вернул" % (marker, head, kind)
    if code and kind == "weather":
        return "%s %s\n%s" % (marker, head, out)
    return "%s %s\n%s" % (marker, head, out)


def remind_reply(arg, cfg):
    """remind: записать в расписание и подтвердить одной строкой."""
    marker = str(cfg.get("marker") or "VIORA")
    code, out = RUNNER("schedule.py", ["add", arg], timeout=60)
    if code:
        return "%s НАПОМИНАНИЕ\n%s" % (marker, out or "не понял заявку")
    _code, listing = RUNNER("schedule.py", ["list"], timeout=60)
    return "%s НАПОМИНАНИЕ\n%s\n\n%s" % (marker, out, listing or "")


def reply_for(cmd, cfg):
    """Единая точка: какой текст сторож отвечает на команду с kind.

    Возвращает (текст, нужен ли мозг). Мгновенные ответы отдаются сразу,
    для мозга сначала уходит THINKING, потом сам ответ. Заявки на пост
    идут отдельной дорогой через brain_reply с заявкой, смотри handle_message.
    """
    kind = cmd.get("kind")
    arg = cmd.get("arg") or ""
    media = cmd.get("media") or []
    if kind in ("pc", "weather", "morning"):
        return life_reply(kind, arg, cfg), False
    if kind == "remind":
        return remind_reply(arg, cfg), False
    if kind == "solve":
        return brain_reply(arg, cfg, "solve", media), True
    if kind == "request":
        return brain_reply(arg, cfg, "request", media), True
    return "", False


# ------------------------------------------------------------------ планировщик

def schedule_due(now_text=""):
    """Что пора выполнить по schedule.py due. Пусто, если ничего или скрипт молчит."""
    argv = ["due", "--json"]
    if now_text:
        argv += ["--now", now_text]
    code, out = RUNNER("schedule.py", argv, timeout=60)
    if code or not out:
        return []
    try:
        data = json.loads(out[out.find("{"):])
    except ValueError:
        return []
    return list(data.get("due") or [])


def schedule_mark(item_id, at_text=""):
    argv = ["mark", str(item_id)]
    if at_text:
        argv += ["--at", at_text]
    RUNNER("schedule.py", argv, timeout=60)


def task_text(item, cfg):
    """Текст сообщения для задачи расписания."""
    marker = str(cfg.get("marker") or "VIORA")
    kind = item.get("kind")
    if kind in ("morning", "weather", "pc"):
        return life_reply(kind, "", cfg)
    return "%s НАПОМИНАНИЕ\n%s" % (marker, item.get("text") or item.get("title") or "")


def minutes_since(last_tick, now):
    """Минуты, пропущенные между тиками: сон компьютера или долгий обрыв."""
    if not last_tick:
        return []
    gap = int((now - last_tick).total_seconds() // 60)
    if gap <= 1:
        return []
    if gap > CATCHUP_HOURS * 60:
        return None
    return [last_tick + datetime.timedelta(minutes=step) for step in range(1, gap)]


def catchup_moments(last_tick, now):
    """Какие минуты догонять после сна. None значит слишком давно, пропускаем."""
    return minutes_since(last_tick, now)


# ---------------------------------------------------------------------- медиа

def media_dir(msg_id):
    path = os.path.join(TG.paths()["media"], str(int(msg_id)))
    os.makedirs(path, exist_ok=True)
    return path


def media_size_of(message):
    """Размер вложения по метаданным, чтобы не качать гигабайты."""
    doc = getattr(message, "document", None)
    if doc is not None:
        return int(getattr(doc, "size", 0) or 0)
    if getattr(message, "photo", None) is not None:
        return 1
    return 0


async def download_media(client, message, folder):
    """Фото или документ до MEDIA_LIMIT в папку. Возвращает путь или пусто."""
    size = media_size_of(message)
    if not size:
        return ""
    if size > MEDIA_LIMIT:
        log("вложение %d МБ больше лимита, пропускаю" % (size // (1024 * 1024)))
        return ""
    try:
        path = await client.download_media(message, file=folder + os.sep)
    except Exception as err:
        log("вложение не скачалось: %s" % err)
        return ""
    return str(path or "")


# --------------------------------------------------------------------- доктор

def telethon_parts():
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    return TelegramClient, events, StringSession


def doctor():
    rows, info, have_telethon = TG.doctor_rows()
    print("Сторож Избранного, tg_watch.py %s" % VERSION)
    print("")
    for what, how, detail in rows:
        print("  %-16s %-4s %s" % (what, how, detail))
    cfg = TG.load_config()
    print("  %-16s %-4s %s" % ("ответ реплаем", "ок", "ловлю" if cfg.get("reply_capture") else "не ловлю"))
    print("  %-16s %-4s %s" % ("мозг", "ок" if brain_on(cfg) else "выкл",
                               "agent.py, потом ai.py, потом каркас" if brain_on(cfg) else "brain=off"))
    if info["problems"] or not have_telethon:
        print("")
        for item in info["problems"]:
            print("  не хватает: %s" % item)
        print("  одной командой: python3 tools/tg.py setup")
        return 1
    print("")
    print("Готов держать смену.")
    return 0


# ---------------------------------------------------------------------- смена

class Watch(object):
    """Состояние одной смены: клиент, настройки, альбомы в сборке, тики."""

    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.client = None
        self.mine = 0
        self.caught = asyncio.Event()
        self.albums = {}
        self.last_tick = None
        self.stop = asyncio.Event()

    # ------------------------------------------------------------ отправка

    async def send(self, to, text, reply_to=0, at=None):
        """Одна дорога наружу для всего сторожа. Вернёт msg_id или 0."""
        body = fit(text or "")
        if not body.strip():
            return 0, "пустой текст"
        entity = TG.target_of(to, self.cfg)
        kwargs = {"parse_mode": None, "link_preview": bool(self.cfg.get("preview"))}
        if entity != "me":
            kwargs["parse_mode"] = "md"
        if reply_to:
            kwargs["reply_to"] = int(reply_to)
        if at is not None:
            kwargs["schedule"] = at
        try:
            msg = await self.client.send_message(entity, body, **kwargs)
        except Exception as err:
            if "reply_to" in kwargs and "reply" in str(err).lower():
                kwargs.pop("reply_to")
                try:
                    msg = await self.client.send_message(entity, body, **kwargs)
                except Exception as again:
                    return 0, "Телеграм отказал: %s" % again
            else:
                return 0, "Телеграм отказал: %s" % err
        return int(getattr(msg, "id", 0) or 0), ""

    async def reply(self, event, text):
        msg_id, err = await self.send("me", text, reply_to=int(event.id))
        if err:
            log("ответ не ушёл: %s" % err)
        else:
            log("ответил: %s" % TG.brief_of(text))
        return msg_id

    # ------------------------------------------------------------- входящие

    async def on_out(self, event):
        try:
            if int(getattr(event, "chat_id", 0) or 0) != self.mine or not event.is_private:
                return
            grouped = getattr(event.message, "grouped_id", None)
            if grouped:
                await self.collect_album(grouped, event)
                return
            await self.handle_message(event, [event.message])
        except Exception as err:  # сторож не падает из-за одного сообщения
            log("ошибка на сообщении: %s" % err)

    async def collect_album(self, grouped, event):
        """Альбом приходит отдельными сообщениями: ждём и собираем в одну заявку."""
        bucket = self.albums.get(grouped)
        if bucket is None:
            self.albums[grouped] = [event]
            await asyncio.sleep(ALBUM_WAIT)
            events_list = self.albums.pop(grouped, [])
            events_list.sort(key=lambda e: int(e.id))
            lead = None
            for item in events_list:
                if (item.raw_text or "").strip():
                    lead = item
                    break
            lead = lead or events_list[0]
            await self.handle_message(lead, [e.message for e in events_list])
        else:
            bucket.append(event)

    async def handle_message(self, event, messages):
        cfg = self.cfg
        text = event.raw_text or ""
        parent_text = ""
        reply_to = int(getattr(event, "reply_to_msg_id", 0) or 0)
        if reply_to and cfg.get("reply_capture"):
            try:
                parent = await event.get_reply_message()
                parent_text = getattr(parent, "raw_text", "") or ""
            except Exception:
                parent_text = ""
        verdict = TG.decide_capture(text, parent_text, cfg)
        if not verdict:
            return
        quick = quick_answer(text, cfg, verdict)
        if quick:
            await self.reply(event, quick)
            return
        media = []
        for message in messages:
            if media_size_of(message):
                path = await download_media(self.client, message, media_dir(event.id))
                if path:
                    media.append(path)
        row = {"msg_id": int(event.id), "text": text, "kind": verdict, "src": "watch",
               "date": str(getattr(event.message, "date", "")), "reply_to": reply_to,
               "media": media}
        if TG.append_inbox([row]):
            log("взял %s id %d: %s%s" % (verdict, row["msg_id"], TG.brief_of(text),
                                          " +%d файлов" % len(media) if media else ""))
            self.caught.set()
        if verdict != "cmd" or not brain_on(cfg):
            return
        cmd = TG.classify(text, cfg, verdict, media)
        kind = cmd.get("kind")
        if kind in ("pc", "weather", "morning", "remind"):
            loop = asyncio.get_event_loop()
            answer, _ = await loop.run_in_executor(None, reply_for, cmd, cfg)
            if answer:
                await self.reply(event, answer)
                TG.mark_handled(event.id)
            return
        if kind in ("publish", "schedule", "cancel", "edit"):
            await self.handle_control(event, cmd, row)
            return
        if kind not in ("solve", "request"):
            return
        if not cmd.get("arg") and not media:
            return
        await self.reply(event, "%s %s" % (cfg["marker"], THINKING.upper()))
        loop = asyncio.get_event_loop()
        if kind == "solve":
            answer, _ = await loop.run_in_executor(None, reply_for, cmd, cfg)
            if answer and await self.reply(event, answer):
                TG.mark_handled(event.id)
            return
        # Заявку на пост заводим до ответа: карточка черновика несёт её id,
        # а агент в редакторе через next не напишет второй пост на ту же тему.
        item = self.open_request(int(event.id), cmd, row)
        answer = await loop.run_in_executor(None, brain_reply, cmd["arg"], cfg, "request", media, item)
        if not answer:
            return
        msg_id = await self.reply(event, answer)
        if msg_id and answer.startswith("%s ЧЕРНОВИК" % cfg["marker"]):
            self.store_draft(item["id"], card_body(answer), msg_id, item.get("rubric", ""))
        elif answer.startswith("%s КАРКАС" % cfg["marker"]):
            # Поста нет: заявка снова открыта для агента в редакторе через next.
            self.release_request(int(event.id))

    def release_request(self, msg_id):
        """Мозг не собрал пост: снимаем пометку handled, next отдаст заявку агенту.

        Сама заявка остаётся: next узнаёт её по msg_id и второй не заводит.
        """
        TG.unmark_handled(msg_id)

    def open_request(self, msg_id, cmd, row):
        """Заявка из строки ящика: своя, если сторож дошёл до неё первым.

        Курсор моста не трогаем: строки до этой агент ещё мог не забрать.
        Пометка handled сама уберёт заявку из выдачи next.
        """
        st = TG.load_state()
        item = TG.item_by_msg(st, msg_id)
        if item is not None:
            return item
        item = TG.new_item(st, row, cmd.get("arg") or row.get("text", ""))
        TG.save_state(st)
        TG.mark_handled(msg_id)
        return item

    async def handle_control(self, event, cmd, row):
        """«пуб», «отложи», «стоп», «правь» к черновику, который собрал сам сторож.

        Заявки агента из редактора не трогаем: они уезжают через next как раньше.
        Своя заявка узнаётся по черновику в drafts и статусу await.
        """
        cfg = self.cfg
        st = TG.load_state()
        target = TG.target_for(st, row, cmd)
        if target is None or target.get("status") not in ("await", "staged"):
            return
        own = bool(target.get("draft")) and target.get("draft_msg_id") and \
            os.path.exists(target.get("draft") or "")
        if not own:
            return
        kind = cmd.get("kind")
        if kind == "cancel":
            TG.touch(target, status="cancelled", note="отменил автор")
            TG.save_state(st)
            TG.mark_handled(event.id)
            await self.reply(event, "%s ОТМЕНА %s" % (cfg["marker"], target["id"]))
            return
        if kind == "edit":
            if not (AGENT_READY() or READY()):
                return  # мозга нет: правку заберёт агент в редакторе через next
            # Правку черновика мозгом делаем той же дорогой, что и новую заявку.
            TG.touch(target, status="work", note=(cmd.get("arg") or "")[:200])
            TG.save_state(st)
            TG.mark_handled(event.id)
            await self.reply(event, "%s %s" % (cfg["marker"], THINKING.upper()))
            old = open(target["draft"], encoding="utf-8").read().strip()
            ask = "Перепиши этот пост по замечанию автора: %s\n\nПост:\n%s" % (cmd.get("arg"), old)
            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(None, brain_reply, ask, cfg, "request", [], target)
            if answer:
                msg_id = await self.reply(event, answer)
                if msg_id and answer.startswith("%s ЧЕРНОВИК" % cfg["marker"]):
                    self.store_draft(target["id"], card_body(answer), msg_id)
            return
        at = None
        if kind == "schedule" or cfg.get("mode") == "scheduled":
            try:
                at = TG.next_slot(cfg, cmd.get("arg") or None)
            except (ValueError, TypeError):
                await self.reply(event, "%s время не разобрал, пиши так: отложи 20:30" % cfg["marker"])
                return
        text = open(target["draft"], encoding="utf-8").read().strip("\n")
        msg_id, err = await self.send("channel", text, 0, at)
        if err:
            await self.reply(event, "%s НЕ ВЫШЛО\nв канал не ушло: %s" % (cfg["marker"], err))
            return
        link = TG.link_for(TG.target_of("channel", cfg), msg_id)
        st = TG.load_state()
        item = TG.find(st, target["id"])
        if item is not None:
            TG.touch(item, status="scheduled" if at else "published",
                     published_msg_id=msg_id, link=link)
            TG.save_state(st)
        TG.mark_handled(event.id)
        word = "ОТЛОЖЕН" if at else "ГОТОВО"
        await self.reply(event, "%s %s %s\n%s" % (cfg["marker"], word, target["id"],
                                                 link or "ссылки нет, канал приватный"))

    def store_draft(self, req, body, draft_msg_id, rubric=""):
        """Черновик от мозга ушёл автору: текст в drafts, статус await, «пуб» найдёт цель."""
        st = TG.load_state()
        item = TG.find(st, req)
        if item is None:
            return False
        if body:
            dest = os.path.join(TG.paths()["drafts"], "%s.txt" % item["id"])
            TG.write_atomic(dest, body + "\n")
            item["draft"] = dest
        if rubric and not item.get("rubric"):
            item["rubric"] = rubric
        TG.touch(item, status="await", draft_msg_id=int(draft_msg_id), tries=0)
        TG.save_state(st)
        return True

    # -------------------------------------------------------------- outbox

    async def outbox_tick(self):
        for row in TG.outbox_pending():
            at = None
            if row.get("at"):
                try:
                    at = datetime.datetime.fromisoformat(row["at"])
                except ValueError:
                    at = None
            msg_id, err = await self.send(row.get("to") or "me", row.get("text") or "",
                                          int(row.get("reply_to") or 0), at)
            link = TG.link_for(TG.target_of(row.get("to") or "me", self.cfg), msg_id) if msg_id else ""
            TG.outbox_done(row, msg_id=msg_id, error=err, link=link)
            if err:
                log("outbox %s не ушёл: %s" % (row.get("role"), err))
                continue
            log("outbox %s ушёл в %s: id %d" % (row.get("role"), row.get("to"), msg_id))
            self.note_sent(row, msg_id, link)

    def note_sent(self, row, msg_id, link):
        """Сам отмечаем заявку по роли записки: агенту не надо помнить про id."""
        req = row.get("req") or ""
        if not req:
            return
        st = TG.load_state()
        item = TG.find(st, req)
        if item is None:
            return
        role = row.get("role")
        if role == "draft":
            TG.touch(item, status="await", draft_msg_id=msg_id, tries=0)
        elif role == "ask":
            TG.touch(item, status="asked", ask_msg_id=msg_id)
        elif role == "publish":
            TG.touch(item, status="scheduled" if row.get("at") else "published",
                     published_msg_id=msg_id, link=link)
        else:
            return
        TG.save_state(st)

    # ------------------------------------------------------------- расписание

    async def schedule_tick(self, now=None):
        now = now or datetime.datetime.now()
        moments = catchup_moments(self.last_tick, now)
        if moments is None:
            log("спал больше %d часов, пропущенные напоминания не догоняю" % CATCHUP_HOURS)
            moments = []
        for moment in moments + [now]:
            stamp = moment.strftime("%Y-%m-%dT%H:%M")
            loop = asyncio.get_event_loop()
            due = await loop.run_in_executor(None, schedule_due, stamp)
            for item in due:
                text = await loop.run_in_executor(None, task_text, item, self.cfg)
                msg_id, err = await self.send("me", text)
                if err:
                    log("напоминание %s не ушло: %s" % (item.get("id"), err))
                    continue
                await loop.run_in_executor(None, schedule_mark, item.get("id"), stamp)
                log("напоминание %s выполнено за %s" % (item.get("id"), stamp))
        self.last_tick = now

    # --------------------------------------------------------------- циклы

    async def heartbeat_loop(self):
        while not self.stop.is_set():
            TG.write_lock(extra={"connected": bool(self.client and self.client.is_connected())})
            await asyncio.sleep(HEARTBEAT_EVERY)

    async def outbox_loop(self):
        while not self.stop.is_set():
            try:
                if self.client is not None and self.client.is_connected():
                    await self.outbox_tick()
            except Exception as err:
                log("outbox: %s" % err)
            await asyncio.sleep(OUTBOX_EVERY)

    async def schedule_loop(self):
        while not self.stop.is_set():
            try:
                if self.client is not None and self.client.is_connected():
                    await self.schedule_tick()
            except Exception as err:
                log("планировщик: %s" % err)
            await asyncio.sleep(SCHEDULE_EVERY)


async def connect_once(watch, info):
    TelegramClient, events, StringSession = telethon_parts()
    client = TelegramClient(
        StringSession(info["session"]), int(info["api_id"]), info["api_hash"],
        device_model=os.environ.get("TELEGRAM_DEVICE_MODEL", "VIORA watch"),
        system_version=os.environ.get("TELEGRAM_SYSTEM_VERSION", "1.0"),
        app_version=os.environ.get("TELEGRAM_APP_VERSION", VERSION),
        auto_reconnect=True, retry_delay=3,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise TG.NetProblem("строка сессии не подошла: python3 tools/tg.py login --force")
    me = await client.get_me()
    watch.client = client
    watch.mine = int(getattr(me, "id", 0))
    client.add_event_handler(watch.on_out, events.NewMessage(outgoing=True))
    return client


async def serve(args):
    global QUIET
    QUIET = bool(getattr(args, "quiet", False))
    alive, row = TG.watcher_alive()
    if alive and row and row.get("pid") != os.getpid():
        text = "сторож уже работает: pid %d, сердцебиение %s назад" % (
            row["pid"], int(time.time() - row["heartbeat"]))
        log(text)
        print("второй экземпляр не нужен. %s" % text)
        return 3
    info = creds()
    if info["problems"]:
        for item in info["problems"]:
            print("не хватает: %s" % item)
        print("одной командой: python3 tools/tg.py setup")
        return 1
    try:
        telethon_parts()
    except Exception:
        print("нет telethon: pip install telethon qrcode")
        return 1

    cfg = TG.load_config()
    TG.ensure_home()
    gone = TG.clean_media(days=MEDIA_DAYS)
    if gone:
        log("убрал %d старых папок media" % gone)
    TG.write_lock()
    watch = Watch(cfg, args)
    tasks = []
    pause = RECONNECT_MIN
    try:
        tasks.append(asyncio.ensure_future(watch.heartbeat_loop()))
        tasks.append(asyncio.ensure_future(watch.outbox_loop()))
        tasks.append(asyncio.ensure_future(watch.schedule_loop()))
        while not watch.stop.is_set():
            try:
                client = await connect_once(watch, info)
            except TG.NetProblem as err:
                log(str(err))
                print(str(err))
                return 1
            except Exception as err:
                log("нет связи: %s, повтор через %d с" % (err, pause))
                await asyncio.sleep(pause)
                pause = min(pause * 2, RECONNECT_MAX)
                continue
            pause = RECONNECT_MIN
            log("Смена началась. Слушаю Избранное, префикс %s" % ", ".join(cfg["prefixes"]))
            log("Ящик: %s" % TG.paths()["home"])
            if args.once:
                try:
                    await asyncio.wait_for(watch.caught.wait(), timeout=max(5, int(args.timeout)))
                except asyncio.TimeoutError:
                    log("за %d с ничего не пришло" % int(args.timeout))
                await client.disconnect()
                return 0
            try:
                await client.run_until_disconnected()
            except Exception as err:
                log("соединение оборвалось: %s" % err)
            if watch.stop.is_set():
                break
            # Обрыв или сон компьютера: telethon сам не всегда возвращается,
            # поэтому пересобираем клиент с растущей паузой.
            log("переподключаюсь через %d с" % pause)
            try:
                await client.disconnect()
            except Exception:
                pass
            watch.client = None
            await asyncio.sleep(pause)
            pause = min(pause * 2, RECONNECT_MAX)
    finally:
        watch.stop.set()
        for task in tasks:
            task.cancel()
        if watch.client is not None:
            try:
                await watch.client.disconnect()
            except Exception:
                pass
        TG.clear_lock(pid=os.getpid())
        log("смена закончена")
    return 0


# ---------------------------------------------------------------------- тесты

class FakeMessage(object):
    def __init__(self, msg_id, text="", grouped_id=None, photo=None, document=None, reply_to=0):
        self.id = msg_id
        self.message = text
        self.raw_text = text
        self.grouped_id = grouped_id
        self.photo = photo
        self.document = document
        self.reply_to_msg_id = reply_to
        self.date = "2026-09-07T08:00:00"


class FakeDoc(object):
    def __init__(self, size):
        self.size = size


class FakeEvent(object):
    def __init__(self, message, chat_id, parent=None):
        self.message = message
        self.id = message.id
        self.raw_text = message.raw_text
        self.chat_id = chat_id
        self.is_private = True
        self.reply_to_msg_id = message.reply_to_msg_id
        self._parent = parent

    async def get_reply_message(self):
        return self._parent


class FakeSent(object):
    def __init__(self, msg_id):
        self.id = msg_id


class FakeClient(object):
    """Подмена telethon: помнит отправки и скачивания, сети не касается."""

    def __init__(self):
        self.sent = []
        self.downloaded = []
        self.next_id = 1000
        self.fail_reply = False

    def is_connected(self):
        return True

    async def send_message(self, entity, body, **kwargs):
        if self.fail_reply and "reply_to" in kwargs:
            raise RuntimeError("Reply message not found")
        self.next_id += 1
        self.sent.append({"to": entity, "text": body, "kwargs": dict(kwargs), "id": self.next_id})
        return FakeSent(self.next_id)

    async def download_media(self, message, file=""):
        path = os.path.join(file.rstrip(os.sep), "photo_%d.jpg" % message.id)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("img")
        self.downloaded.append(path)
        return path


def selftest():
    global RUNNER, READY, AGENT_READY, LOCAL_ALIVE, QUIET
    checks = []
    QUIET = True

    def ok(name, cond):
        checks.append((name, bool(cond)))

    cfg = dict(TG.DEFAULT_CONFIG)
    ok("ядро моста подгружается", hasattr(TG, "append_inbox") and hasattr(TG, "decide_capture"))
    ok("версии совпадают", TG.VERSION == VERSION)
    ok("личное не берём", TG.decide_capture("напомнить про зал", "", cfg) is None)
    ok("заявку берём", TG.decide_capture("/viora пост про Notion", "", cfg) == "cmd")
    ok("ответ реплаем берём", TG.decide_capture("да, так и было", "VIORA ВОПРОС r3", cfg) == "answer")

    old = {}
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "VIORA_SECRETS",
                "VIORA_TG_HOME") + TG.SESSION_ENVS:
        old[key] = os.environ.pop(key, None)
    tmp = tempfile.mkdtemp(prefix="viora-watch-")
    keep = (RUNNER, READY, AGENT_READY, LOCAL_ALIVE)
    try:
        os.environ["VIORA_SECRETS"] = os.path.join(tmp, "secrets")
        os.environ["VIORA_TG_HOME"] = os.path.join(tmp, "state")
        TG.ensure_home()
        empty = creds()
        ok("без доступов говорим три вещи", len(empty["problems"]) == 3)
        TG.write_atomic(
            os.path.join(tmp, "secrets", "watch.env"),
            "TELEGRAM_API_ID=123456\nTELEGRAM_API_HASH=hash\n"
            "TELEGRAM_SESSION_STRING_WATCH=own\n")
        full = creds()
        ok("ключи и сессия берутся из файла", not full["problems"] and full["session"] == "own")
        ok("на проверку связи отвечаем сразу",
           quick_answer("/viora как дела", cfg, "cmd").startswith(cfg["marker"]))
        ok("помощь отдаём сразу", "ПОМОЩЬ" in quick_answer("/viora", cfg, "cmd"))
        ok("заявку быстрым ответом не гасим", quick_answer("/viora пост про Notion", cfg, "cmd") == "")
        ok("ответ реплаем отдаём агенту", quick_answer("да, так и было", cfg, "answer") == "")

        # лог с ротацией
        log_path = TG.paths()["log"]
        log("первая строка")
        ok("лог пишется", os.path.exists(log_path) and "первая строка" in open(log_path, encoding="utf-8").read())
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("x" * (LOG_LIMIT + 10))
        log("после ротации")
        ok("лог ротируется на 1 МБ",
           os.path.exists(log_path + ".1") and os.path.getsize(log_path) < 1000)

        # Мозг: куда уходит свободный текст и что возвращается в телегу
        ok("заявку на пост узнаём", wants_post("пост про халявный доступ к моделям"))
        ok("ссылка тоже заявка", wants_post("https://example.com глянь и сделай"))
        ok("разговор постом не считаем", not wants_post("объясни, почему так вышло"))
        ok("мозг включён по умолчанию", brain_on(cfg))
        ok("мозг выключается конфигом", not brain_on(dict(cfg, brain="off")))

        # ключ модели: без ключа не готовы, локальная модель готова только с живым портом
        old_ai = {k: os.environ.pop(k, None) for k in ("VIORA_AI_PROVIDER", "VIORA_AI_KEY", "OPENROUTER_API_KEY")}
        try:
            ok("без ключа ai.py не готов", not ai_ready())
            os.environ["VIORA_AI_PROVIDER"] = "ollama"
            LOCAL_ALIVE = lambda base: False
            ok("ollama без процесса на порту не считается мозгом", not ai_ready())
            LOCAL_ALIVE = lambda base: "11434" in base
            ok("ollama с живым портом готова", ai_ready())
            os.environ.pop("VIORA_AI_PROVIDER", None)
            os.environ["OPENROUTER_API_KEY"] = "test"
            LOCAL_ALIVE = lambda base: False
            ok("с ключом порт не спрашиваем", ai_ready())
        finally:
            for k, v in old_ai.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            LOCAL_ALIVE = keep[3]
        ok("пробник порта не падает на пустом адресе", local_alive("") in (True, False))
        ok("пробник порта видит закрытый порт", not local_alive("http://127.0.0.1:1"))

        calls = []
        # Гейт линтера в тестах: файл с «слоп» не проходит, остальное чисто.
        lint_state = {"fail_once": False, "seen": []}

        def fake_lint(args):
            path = args[0]
            text = open(path, encoding="utf-8").read()
            lint_state["seen"].append(text.strip())
            if "--fix" in args:
                open(path, "w", encoding="utf-8").write(text.replace("\u2014", "-"))
                return 0, "Автоправка"
            if "слоп" in text or lint_state["fail_once"]:
                lint_state["fail_once"] = False
                return 1, json.dumps({"errors": [{"code": "E-SLOP-CONSTRUCT", "message": "слоп-конструкция"}],
                                      "warns": []})
            return 0, json.dumps({"clean": True, "errors": [], "warns": []})

        def fake_runner(script, args, timeout=0):
            calls.append((script, [str(a) for a in args]))
            if script == "agent.py":
                task_path = args[args.index("--task") + 1]
                task = json.load(open(task_path, encoding="utf-8"))
                calls[-1] = (script, [str(a) for a in args], task)
                reply = "ответ агента по %s" % task["kind"]
                if task["kind"] == "request" and "безнадёжн" in task["text"]:
                    reply = "слоп от агента"
                elif task["kind"] == "request" and "забраковал" in task["text"]:
                    reply = "ответ агента со второго захода"
                elif task["kind"] == "request" and "слоп" in task["text"]:
                    reply = "слоп от агента"
                return 0, json.dumps({"ok": True, "text": reply, "agent": "claude"})
            if script == "lint_post.py":
                return fake_lint([str(a) for a in args])
            if script == "life.py":
                return 0, "ПК test, свободно 10 ГБ"
            if script == "schedule.py":
                if args[0] == "add":
                    return 0, "Записал 1: каждый день 08:00 утренний дайджест"
                if args[0] == "list":
                    return 0, "Напоминаний: 1"
                if args[0] == "due":
                    return 0, json.dumps({"due": [{"id": 1, "kind": "morning", "title": "утро"}]})
                return 0, "ok"
            if script == "post.py" and "--json" in args:
                post_text = "слоп от модели" if "безнадёжн" in str(args[0]) else "готовый текст поста"
                return 0, json.dumps({"post": post_text, "brief": "бриф", "rubric": "абуз",
                                      "skeleton": "скелет", "passed": True})
            return 0, "готовый текст"

        RUNNER = fake_runner
        AGENT_READY = lambda: True
        READY = lambda: False

        answer = brain_reply("реши 2+2", cfg, "solve", ["/tmp/a.jpg"])
        ok("solve уходит в agent.py", calls and calls[-1][0] == "agent.py")
        ok("agent.py получает kind и media",
           calls[-1][2]["kind"] == "solve" and calls[-1][2]["media"] == ["/tmp/a.jpg"])
        ok("ответ агента с маркером РЕШЕНИЕ", answer.startswith("VIORA РЕШЕНИЕ"))
        ok("ответ агента в ящик не вернётся", TG.decide_capture(answer, "", cfg) is None)
        answer = brain_reply("пост про халяву", cfg, "request")
        last_agent = lambda: [c for c in calls if c[0] == "agent.py"][-1]
        ok("request уходит в agent.py как пост", last_agent()[2]["kind"] == "request")
        ok("пост от агента это ЧЕРНОВИК с телом после пустой строки",
           answer.startswith("VIORA ЧЕРНОВИК") and card_body(answer) == "ответ агента по request")
        fake_item = {"id": "r9", "rubric": "абуз"}
        answer = brain_reply("пост про халяву", cfg, "request", None, fake_item)
        ok("с заявкой ответ это карточка stage с id и словом пуб",
           answer.startswith("VIORA ЧЕРНОВИК r9") and "пуб" in answer
           and card_body(answer) == "ответ агента по request")
        lint_calls = [c for c in calls if c[0] == "lint_post.py"]
        ok("пост от агента идёт через гейт линтера: --fix, потом --strict",
           len(lint_calls) >= 2 and "--fix" in lint_calls[-2][1] and "--strict" in lint_calls[-1][1])
        ok("гейт знает рубрику заявки", "абуз" in lint_calls[-1][1])
        # первый вариант забракован: второй заход с претензиями, автору уходит чистый
        del calls[:]
        answer = brain_reply("пост со слоп-словами", cfg, "request", None, fake_item)
        agent_calls = [c for c in calls if c[0] == "agent.py"]
        ok("забракованный пост уходит агенту на второй заход", len(agent_calls) == 2
           and "E-SLOP-CONSTRUCT" in agent_calls[1][2]["text"])
        ok("автору ушёл вариант со второго захода",
           card_body(answer) == "ответ агента со второго захода")
        # оба захода грязные: поста нет, автору каркас и заявка агенту в редакторе
        del calls[:]
        answer = brain_reply("безнадёжный пост", cfg, "request", None, fake_item)
        ok("после двух грязных заходов черновика нет", not answer.startswith("VIORA ЧЕРНОВИК"))
        ok("автор видит, что линтер не пустил", "не прошёл линтер" in answer and "tg.py next" in answer)
        # рубрика без заявки угадывается по тексту, а не остаётся пустой
        del calls[:]
        no_rubric = {"id": "r10", "rubric": ""}
        brain_reply("пост про бесплатные кредиты", cfg, "request", None, no_rubric)
        lint_calls = [c for c in calls if c[0] == "lint_post.py"]
        ok("рубрика угадана по заявке", no_rubric["rubric"] == "абуз" and "абуз" in lint_calls[-1][1])
        answer = brain_reply("как думаешь, стоит ли", cfg)
        ok("свободный текст это разговор", calls[-1][2]["kind"] == "talk" and "ОТВЕТ" in answer)

        # агента нет, ключа нет: каркас
        AGENT_READY = lambda: False
        del calls[:]
        answer = brain_reply("пост про халяву в кодинге", cfg)
        ok("без агента и ключа цепочку зовём без модели", calls and calls[0][1][-1] == "--no-ai")
        ok("без ключа отдаём каркас", "КАРКАС" in answer and "tg.py next" in answer)
        del calls[:]
        answer = brain_reply("объясни, почему так вышло", cfg)
        ok("без мозга говорим, что поставить", not calls and "claude, codex, agy" in answer)

        # агента нет, ключ есть: ai.py и post.py --json
        READY = lambda: True
        del calls[:]
        answer = brain_reply("объясни, почему так вышло", cfg)
        ok("с ключом зовём ai.py", calls and calls[0][0] == "ai.py")
        ok("ответ ai.py с маркером", answer.startswith("VIORA ОТВЕТ"))
        del calls[:]
        answer = brain_reply("пост про халяву", cfg, "request", None, fake_item)
        ok("с ключом пост идёт через post.py --json",
           calls[0][0] == "post.py" and "--json" in calls[0][1]
           and card_body(answer) == "готовый текст поста")
        ok("пост от ai.py тоже идёт через гейт линтера",
           any(c[0] == "lint_post.py" and "--strict" in c[1] for c in calls))
        lint_state["fail_once"] = True
        del calls[:]
        answer = brain_reply("пост про халяву", cfg, "request", None, fake_item)
        ok("грязный пост от ai.py автору не уходит", "КАРКАС" in answer and "линтер" in answer)

        # агент есть, но молчит: падаем на ai.py
        AGENT_READY = lambda: True

        def silent_agent(script, args, timeout=0):
            calls.append((script, [str(a) for a in args]))
            if script == "agent.py":
                return 1, json.dumps({"ok": False, "why": "claude: Not logged in"})
            return 0, "ответ модели"

        RUNNER = silent_agent
        del calls[:]
        answer = brain_reply("объясни", cfg)
        ok("молчащий агент уступает ai.py",
           [c[0] for c in calls] == ["agent.py", "ai.py"] and "ответ модели" in answer)

        # мгновенные ответы без модели
        RUNNER = fake_runner
        text, slow = reply_for({"kind": "pc", "arg": "", "media": []}, cfg)
        ok("pc отвечает life.py мгновенно", not slow and text.startswith("VIORA ПК") and calls[-1][0] == "life.py")
        text, slow = reply_for({"kind": "weather", "arg": "Казань", "media": []}, cfg)
        ok("weather передаёт город", calls[-1][1] == ["weather", "Казань"])
        text, slow = reply_for({"kind": "remind", "arg": "08:00 утро", "media": []}, cfg)
        ok("remind зовёт schedule.py add и list",
           [c[1][0] for c in calls[-2:]] == ["add", "list"] and text.startswith("VIORA НАПОМИНАНИЕ"))
        text, slow = reply_for({"kind": "solve", "arg": "реши", "media": []}, cfg)
        ok("solve помечен как медленный", slow)

        # планировщик: догоняем пропущенные минуты, но не старше трёх часов
        now = datetime.datetime(2026, 9, 7, 8, 5)
        ok("без прошлого тика догонять нечего", catchup_moments(None, now) == [])
        ok("минута назад: без догона", catchup_moments(now - datetime.timedelta(minutes=1), now) == [])
        moments = catchup_moments(now - datetime.timedelta(minutes=10), now)
        ok("десять минут сна: девять минут догона", len(moments) == 9 and moments[0].minute == 56)
        ok("больше трёх часов: пропускаем",
           catchup_moments(now - datetime.timedelta(hours=4), now) is None)
        ok("due разбирается", schedule_due("2026-09-07T08:00")[0]["kind"] == "morning")

        # сторож с фейковым клиентом: входящие, медиа, альбом, outbox, расписание
        TG.save_config(dict(TG.DEFAULT_CONFIG))
        watch = Watch(TG.load_config(), argparse.Namespace(once=False, timeout=5, quiet=True))
        client = FakeClient()
        watch.client = client
        watch.mine = 777

        async def scenario():
            # чужой чат не трогаем
            await watch.on_out(FakeEvent(FakeMessage(1, "/viora пуб"), chat_id=555))
            ok("чужой чат не ловим", not client.sent and not TG.read_inbox())
            # проверка связи: ответ реплаем сразу
            await watch.on_out(FakeEvent(FakeMessage(2, "виора как дела"), chat_id=777))
            ok("ping отвечен реплаем", client.sent and client.sent[-1]["kwargs"].get("reply_to") == 2)
            ok("ping не лёг в ящик", not TG.read_inbox())
            # фото плюс реши: скачали, взяли, подумали, ответили
            del calls[:]
            await watch.on_out(FakeEvent(FakeMessage(3, "виора реши", photo=object()), chat_id=777))
            rows = TG.read_inbox()
            ok("solve с фото лёг в ящик с media", rows and rows[-1]["media"] and rows[-1]["msg_id"] == 3)
            ok("фото скачано в media/<msg_id>", client.downloaded and os.sep + "3" + os.sep in client.downloaded[0])
            texts = [s["text"] for s in client.sent]
            ok("сначала «взял, думаю», потом ответ",
               any(THINKING.upper() in t for t in texts) and texts[-1].startswith("VIORA РЕШЕНИЕ"))
            ok("агент получил путь к фото", calls and calls[-1][0] == "agent.py" and calls[-1][2]["media"])
            # альбом: три сообщения с одним grouped_id, одна заявка
            before = len(TG.read_inbox())
            big = FakeDoc(MEDIA_LIMIT + 1)
            e1 = FakeEvent(FakeMessage(10, "", grouped_id=42, photo=object()), chat_id=777)
            e2 = FakeEvent(FakeMessage(11, "виора реши", grouped_id=42, photo=object()), chat_id=777)
            e3 = FakeEvent(FakeMessage(12, "", grouped_id=42, document=big), chat_id=777)
            await asyncio.gather(watch.on_out(e1), watch.on_out(e2), watch.on_out(e3))
            rows = TG.read_inbox()
            ok("альбом стал одной заявкой", len(rows) == before + 1)
            ok("у альбома два файла, большой документ пропущен", len(rows[-1]["media"]) == 2)
            ok("подпись альбома взята из сообщения с текстом", rows[-1]["text"] == "виора реши")
            # pc без модели: ответ есть, агенту через next строка не отдаётся
            await watch.on_out(FakeEvent(FakeMessage(20, "виора пк"), chat_id=777))
            ok("pc отвечен без «думаю»", client.sent[-1]["text"].startswith("VIORA ПК"))
            ok("pc помечен handled", [r for r in TG.read_inbox() if r["msg_id"] == 20][0].get("handled"))
            # remind
            await watch.on_out(FakeEvent(FakeMessage(21, "виора напоминай 08:00 утро"), chat_id=777))
            ok("remind подтверждён", client.sent[-1]["text"].startswith("VIORA НАПОМИНАНИЕ"))
            # request через мозг: заявка своя, черновик в drafts, next её не отдаёт
            await watch.on_out(FakeEvent(FakeMessage(30, "виора пост про халяву в Notion"), chat_id=777))
            card = client.sent[-1]["text"]
            ok("черновик от мозга ушёл карточкой stage", card.startswith("VIORA ЧЕРНОВИК r") and "пуб" in card)
            item = [i for i in TG.load_state()["items"] if i.get("msg_id") == 30][0]
            ok("заявка заведена сторожем и ждёт добро",
               item["status"] == "await" and item["draft_msg_id"] == client.sent[-1]["id"])
            ok("текст черновика лежит в drafts",
               item.get("draft") and open(item["draft"], encoding="utf-8").read().strip() == "ответ агента по request")
            got = TG.take_next(TG.load_state(), TG.load_config())
            seen = []
            while got:
                seen.append(got.get("msg_id"))
                got = TG.take_next(TG.load_state(), TG.load_config())
            ok("next не отдаёт агенту то, что сторож закрыл сам", not seen)
            # «виора правь» переписывает черновик мозга, «виора пуб» публикует его сам сторож
            await watch.on_out(FakeEvent(FakeMessage(31, "виора правь короче"), chat_id=777))
            ok("правь переписан мозгом", client.sent[-1]["text"].startswith("VIORA ЧЕРНОВИК %s" % item["id"]))
            ok("правка дошла до агента", "короче" in last_agent()[2]["text"])
            await watch.on_out(FakeEvent(FakeMessage(33, "виора пуб"), chat_id=777))
            ok("пуб ушёл в канал", client.sent[-2]["to"] == "@VioraStudio"
               and client.sent[-2]["text"] == "ответ агента по request")
            ok("автору пришло ГОТОВО со ссылкой",
               client.sent[-1]["text"].startswith("VIORA ГОТОВО %s" % item["id"]) and "t.me/VioraStudio" in client.sent[-1]["text"])
            item = TG.find(TG.load_state(), item["id"])
            ok("заявка мозга закрыта как опубликованная", item["status"] == "published" and item["published_msg_id"] == client.sent[-2]["id"])
            ok("next не отдаёт пуб агенту повторно", TG.take_next(TG.load_state(), TG.load_config()) is None)
            # reply на свой вопрос: answer в ящик, без мозга, агент видит его через next
            parent = FakeMessage(900, "VIORA ВОПРОС r1\nкакой факт")
            await watch.on_out(FakeEvent(FakeMessage(34, "со второго раза", reply_to=900), chat_id=777, parent=parent))
            ok("ответ реплаем лёг как answer", TG.read_inbox()[-1]["kind"] == "answer")
            got = TG.take_next(TG.load_state(), TG.load_config())
            ok("next отдаёт ответ реплаем агенту", got and got["kind"] == "answer" and got["msg_id"] == 34)
            # мозг не собрал чистый пост: автору каркас, заявка уходит агенту через next той же id
            await watch.on_out(FakeEvent(FakeMessage(35, "виора безнадёжный пост про слоп"), chat_id=777))
            ok("после грязного поста автору ушёл каркас", client.sent[-1]["text"].startswith("VIORA КАРКАС"))
            got = TG.take_next(TG.load_state(), TG.load_config())
            ok("next отдаёт агенту заявку, которую мозг не вытянул",
               got and got["kind"] == "request" and got["msg_id"] == 35)
            same = [i for i in TG.load_state()["items"] if i.get("msg_id") == 35]
            ok("заявка одна, а не две на одну тему", len(same) == 1 and got and got["id"] == same[0]["id"])
            # пуб к чужой заявке (агента из редактора) сторож не трогает
            st = TG.load_state()
            foreign = TG.new_item(st, {"msg_id": 40, "text": "тема"}, "тема")
            TG.touch(foreign, status="await", draft_msg_id=5555)
            TG.save_state(st)
            sent_before = len(client.sent)
            await watch.on_out(FakeEvent(FakeMessage(41, "виора пуб"), chat_id=777))
            ok("пуб к заявке агента сторож не трогает", len(client.sent) == sent_before)
            got = TG.take_next(TG.load_state(), TG.load_config())
            ok("пуб к заявке агента уходит агенту через next", got and got["kind"] == "publish" and got["id"] == foreign["id"])
            # outbox: записки от tg.py уходят через сторожа и отмечаются сами
            TG.write_lock()
            row = TG.outbox_put("VIORA ЧЕРНОВИК %s\nтекст" % item["id"], "me", 30, None, item["id"], "draft")
            row2 = TG.outbox_put("пост в канал", "channel", 0, None, item["id"], "publish")
            await watch.outbox_tick()
            done = {d["id"]: d for d in TG.read_jsonl(TG.paths()["outbox_done"])}
            ok("outbox: обе записки подтверждены", row["id"] in done and row2["id"] in done)
            ok("outbox: msg_id вернулся", done[row["id"]]["msg_id"] > 0)
            ok("outbox: канал получил md", client.sent[-1]["to"] == "@VioraStudio" and client.sent[-1]["kwargs"]["parse_mode"] == "md")
            item = TG.find(TG.load_state(), item["id"])
            ok("outbox: publish сам закрыл заявку", item["status"] == "published" and item["link"].endswith(str(client.sent[-1]["id"])))
            ok("outbox: очередь пуста", TG.outbox_pending() == [])
            # отправка с битым reply_to не теряет текст
            client.fail_reply = True
            msg_id, err = await watch.send("me", "текст", reply_to=5)
            ok("битый reply_to не теряет текст", msg_id and not err and "reply_to" not in client.sent[-1]["kwargs"])
            client.fail_reply = False
            # расписание: due выполняется, mark зовётся
            del calls[:]
            await watch.schedule_tick(datetime.datetime(2026, 9, 7, 8, 0))
            ok("планировщик отправил утро", client.sent[-1]["text"].startswith("VIORA УТРО"))
            ok("планировщик отметил задачу", any(c[0] == "schedule.py" and c[1][0] == "mark" for c in calls))
            ok("тик запомнен", watch.last_tick == datetime.datetime(2026, 9, 7, 8, 0))

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())

        # второй экземпляр при живом lock
        TG.write_lock(pid=999999, extra={"heartbeat": time.time()})
        code = asyncio.new_event_loop().run_until_complete(
            serve(argparse.Namespace(once=True, timeout=5, quiet=True)))
        ok("второй экземпляр не стартует", code == 3)
        TG.clear_lock()
    finally:
        RUNNER, READY, AGENT_READY, LOCAL_ALIVE = keep
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    ok("длинный ответ влезает в сообщение", len(fit("я" * (TG.TG_LIMIT + 500))) <= TG.TG_LIMIT)
    ok("короткий ответ не трогаем", fit("коротко") == "коротко")

    QUIET = False
    bad = [name for name, good in checks if not good]
    for name, good in checks:
        print("%s %s" % ("PASS" if good else "FAIL", name))
    print("Итого: %d проверок сторожа, провалилось %d" % (len(checks), len(bad)))
    return 1 if bad else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="Сторож Избранного для VIORA STUDIO")
    ap.add_argument("--once", action="store_true", help="выйти после первой заявки")
    ap.add_argument("--timeout", type=int, default=600, help="сколько ждать в режиме --once")
    ap.add_argument("--quiet", action="store_true", help="без консоли, только watch.log")
    ap.add_argument("--doctor", action="store_true", help="проверить доступы")
    ap.add_argument("--selftest", action="store_true", help="свои тесты")
    ap.add_argument("--version", action="version", version="tg_watch.py %s" % VERSION)
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.doctor:
        return doctor()
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        TG.clear_lock(pid=os.getpid())
        print("смена закончена")
        return 0


if __name__ == "__main__":
    sys.exit(main())
