#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent.py 1.0.0

Мозг без ключей: подписочный CLI агента на этой машине отвечает на задачу автора.

Зачем. Автор платит за Claude Code, Codex, Antigravity или Gemini CLI и хочет,
чтобы вопрос из Телеграма решала та же модель, а не ещё один ключ в secrets/.
У всех четырёх есть безголовый режим: команда, промпт, ответ в stdout. Этот файл
находит установленный CLI, собирает промпт по правилам скилла, запускает его
без окна и отдаёт чистый текст. Сторож tools/tg_watch.py зовёт его первым,
ai.py по ключу вторым, каркас без модели третьим.

Команды:
    python3 tools/agent.py which                 какие CLI найдены и кто первый
    python3 tools/agent.py run --task t.json --out answer.txt
    python3 tools/agent.py run --text "сколько будет 2+2" --kind solve
    python3 tools/agent.py --selftest

Файл задачи: {"text": "...", "media": ["/путь/1.jpg"], "kind": "solve", "cwd": "/корень"}
kind: solve (ответ по делу), request (пост по пайплайну скилла), talk (разговор).

Порядок агентов: поле «предпочитаемый агент» в memory/profile.md, значение
auto или пусто значит первый найденный из claude, codex, agy, gemini.

Безопасность. Агент запускается с песочницей своего CLI только на чтение.
Правка файлов открывается лишь когда автор явно пишет «поправь файл» и
в tg-config.json стоит trust=edit. Папку secrets/ агенту читать запрещено
текстом промпта, а строка сессии и ключи в промпт не попадают никогда:
задача несёт только текст автора, kind и пути к картинкам.

Что делать, когда headless-флаг сменился. Все флаги собраны в одном месте,
функция build_argv. Проверь `<cli> --help` и правь строку своего CLI там.
Разбор ответа живёт в parse_output: если CLI стал печатать другой JSON,
правится только эта функция.

Коды возврата: 0 ответ есть, 1 агент не ответил, 2 нет ни одного CLI или ошибка вызова.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
ROOT = os.path.dirname(SKILL)

TIMEOUT = 600
REPLY_LIMIT = 3500
KINDS = ("solve", "request", "talk")
# Порядок по умолчанию, когда профиль молчит или говорит auto.
ORDER = ("claude", "codex", "agy", "gemini")
# Слова автора, после которых агенту разрешают правку файлов (при trust=edit).
EDIT_WORDS = ("поправь файл", "исправь файл", "измени файл", "правь файл",
              "поправь код", "исправь код", "запиши в файл", "отредактируй файл")
DASHES = "\u2014\u2013\u2015\u2012\u2212"


# ------------------------------------------------------------------ соседи

def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def profile_agent():
    """Предпочитаемый агент из профиля автора. Пусто или auto значит по порядку."""
    path = os.path.join(HERE, "memory.py")
    if not os.path.exists(path):
        return ""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("viora_agent_memory", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return (module.profile_get(SKILL, "agent") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def trust_level():
    """trust из настроек моста: read по умолчанию, edit по воле автора."""
    path = os.path.join(HERE, "tg.py")
    if not os.path.exists(path):
        return "read"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("viora_agent_tg", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.load_config().get("trust") or "read").lower()
    except Exception:  # noqa: BLE001
        return "read"


# ------------------------------------------------------------------ поиск CLI

WHICH = shutil.which


def find_clis(which=None):
    """Какие CLI стоят: имя и полный путь, в порядке ORDER."""
    which = which or WHICH
    found = []
    for name in ORDER:
        path = which(name)
        if path:
            found.append((name, path))
    return found


def pick(found, prefer=""):
    """Первый подходящий: сначала предпочтение из профиля, потом по порядку."""
    prefer = (prefer or "").strip().lower()
    if prefer and prefer != "auto":
        for name, path in found:
            if name == prefer:
                return name, path
    return found[0] if found else (None, None)


# ------------------------------------------------------------------ промпт

def wants_edit(text):
    low = " ".join((text or "").lower().split())
    return any(word in low for word in EDIT_WORDS)


def allow_edit(text, trust=None):
    """Правка файлов только по явной просьбе и только при trust=edit."""
    trust = (trust if trust is not None else trust_level()).lower()
    return trust == "edit" and wants_edit(text)


def build_prompt(task, root=None):
    """Промпт для агента: правила скилла ссылками, задача автора, пути к картинкам."""
    root = root or task.get("cwd") or ROOT
    kind = task.get("kind") or "talk"
    text = (task.get("text") or "").strip()
    media = [str(x) for x in (task.get("media") or []) if str(x).strip()]
    lines = [
        "Ты агент скилла VIORA STUDIO. Рабочая папка: %s." % root,
        "Сначала прочитай AGENTS.md и viora-studio/SKILL.md в этой папке и работай по ним.",
        "Папку secrets/ и файлы *.env не открывай и не цитируй никогда: там ключи автора.",
        "",
    ]
    if kind == "request":
        lines += [
            "Задача автора: заявка на пост в канал. Пройди пайплайн скилла целиком:",
            "route, угол, черновик, ship до кода 0, проход-правка. В ответ верни только",
            "готовый текст поста без своих пояснений. Публиковать нельзя, это черновик.",
        ]
    elif kind == "solve":
        lines += [
            "Задача автора: решить или объяснить. Отвечай по делу, шагами, без воды.",
            "Если приложены картинки, сначала посмотри их инструментом чтения файлов.",
        ]
    else:
        lines += ["Задача автора: обычный разговор. Ответь коротко и по-человечески."]
    lines += ["", "Сообщение автора:", text or "(пусто)"]
    if media:
        lines += ["", "Картинки к задаче, открой каждую:"]
        lines += ["- %s" % path for path in media]
    lines += [
        "",
        "Требования к ответу: по-русски, коротко, до %d знаков." % REPLY_LIMIT,
        "Без длинных тире: только дефис, точка, двоеточие. Без markdown-заголовков.",
        "Ответ уйдёт в Телеграм как есть, поэтому никаких вступлений вроде «Конечно».",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ вызовы CLI
# Здесь и только здесь живут флаги безголовых режимов. Проверено 2026-09-07:
#   claude 2.1.x: `claude -p "<prompt>" --output-format json --allowedTools ...`
#     печатает один JSON с полем result. Без stdin ждёт 3 секунды и пишет
#     предупреждение в stdout, поэтому stdin закрываем сразу (DEVNULL).
#   codex: `codex exec [FLAGS] -- "<prompt>"` с --sandbox read-only, -C корень,
#     --output-last-message файл. Флаг -i/--image жадный и глотает промпт,
#     поэтому картинки идут до разделителя `--`, промпт после него.
#   agy: `agy -p "<prompt>" --output-format json`. Без TTY stdout бывает пустым
#     (issue #76 antigravity-cli), тогда повтор через pty на mac и Linux.
#   gemini: `gemini -p "<prompt>" --output-format json`, поле response.

READ_TOOLS = "Read,Glob,Grep,Bash(python3 tools/*)"
EDIT_TOOLS = "Read,Glob,Grep,Edit,Write,Bash(python3 tools/*)"


def build_argv(name, prompt, task, edit=False, out_file=""):
    """argv для CLI. Промпт всегда позиционный, картинки перечислены в промпте."""
    media = [str(x) for x in (task.get("media") or []) if str(x).strip()]
    root = task.get("cwd") or ROOT
    if name == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "json",
                "--allowedTools", EDIT_TOOLS if edit else READ_TOOLS]
        if edit:
            argv += ["--permission-mode", "acceptEdits"]
        return argv
    if name == "codex":
        argv = ["codex", "exec", "-C", root,
                "--sandbox", "workspace-write" if edit else "read-only",
                "--skip-git-repo-check"]
        for path in media:
            argv += ["-i", path]
        if out_file:
            argv += ["--output-last-message", out_file]
        argv += ["--", prompt]
        return argv
    if name == "agy":
        return ["agy", "-p", prompt, "--output-format", "json"]
    if name == "gemini":
        return ["gemini", "-p", prompt, "--output-format", "json"]
    raise ValueError("неизвестный агент: %s" % name)


def run_process(argv, cwd, timeout=TIMEOUT):
    """Запуск без окна и без TTY. Возвращает (код, stdout, stderr)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("NO_COLOR", "1")
    try:
        done = subprocess.run(argv, cwd=cwd or None, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "не уложился в %d секунд" % timeout
    except OSError as err:
        return 127, "", "не запустился: %s" % err
    return (done.returncode,
            (done.stdout or b"").decode("utf-8", "replace"),
            (done.stderr or b"").decode("utf-8", "replace"))


def run_pty(argv, cwd, timeout=TIMEOUT):
    """Повтор через псевдотерминал для CLI, которые молчат без TTY."""
    if os.name == "nt":
        return 125, "", "pty нет на Windows"
    import pty
    import select
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(argv, cwd=cwd or None, stdin=slave, stdout=slave,
                                stderr=subprocess.PIPE, close_fds=True)
    except OSError as err:
        os.close(master)
        os.close(slave)
        return 127, "", "не запустился: %s" % err
    os.close(slave)
    chunks = []
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            proc.kill()
            os.close(master)
            return 124, "", "не уложился в %d секунд" % timeout
        ready, _w, _e = select.select([master], [], [], 0.5)
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if not data:
                break
            chunks.append(data)
        elif proc.poll() is not None:
            break
    os.close(master)
    proc.wait()
    err = (proc.stderr.read() if proc.stderr else b"") or b""
    out = b"".join(chunks).decode("utf-8", "replace")
    out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out).replace("\r", "")
    return proc.returncode, out, err.decode("utf-8", "replace")


# Тесты подменяют эти точки: каждый CLI прогоняется без настоящего запуска.
RUNNER = run_process
PTY_RUNNER = run_pty


def parse_output(name, stdout, out_file=""):
    """Вытащить текст ответа из того, что напечатал CLI. Вернёт (текст, ошибка)."""
    body = (stdout or "").strip()
    if name == "codex":
        if out_file and os.path.exists(out_file):
            text = read_text(out_file).strip()
            if text:
                return text, ""
        # Без файла берём последний assistant_message из JSONL или голый stdout.
        last, failed = "", ""
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            item = row.get("item") or {}
            if item.get("item_type") == "assistant_message" and item.get("text"):
                last = item["text"]
            if row.get("type") == "turn.failed":
                failed = str((row.get("error") or {}).get("message") or "turn.failed")
        if last.strip():
            return last.strip(), ""
        if failed:
            return "", failed
        return body, ""
    if not body:
        return "", ""
    data = None
    try:
        data = json.loads(body)
    except ValueError:
        # Претензии CLI могут стоять перед JSON: ищем первую фигурную скобку.
        cut = body.find("{")
        if cut != -1:
            try:
                data = json.loads(body[cut:])
            except ValueError:
                data = None
    if isinstance(data, dict):
        text = ""
        for key in ("result", "response", "text", "output", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        # claude кладёт причину отказа в то же поле result, но ставит is_error.
        if data.get("is_error") or data.get("error"):
            reason = data.get("error") if isinstance(data.get("error"), str) else text
            return "", str(reason or "CLI вернул ошибку")
        return text, ""
    return body, ""


def clean_reply(text):
    """Правило ноль и чистка ИИ-слов через lint_chat.py --fix, если он рядом."""
    body = (text or "").strip()
    if not body:
        return ""
    path = os.path.join(HERE, "lint_chat.py")
    if os.path.exists(path):
        try:
            done = subprocess.run([sys.executable, path, "--fix", "--text", body],
                                  capture_output=True, text=True, timeout=30)
            if done.returncode == 0 and done.stdout.strip():
                body = done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    for dash in DASHES:
        body = body.replace(" %s " % dash, " - ").replace(dash, "-")
    body = re.sub(r"^#{1,6}\s+", "", body, flags=re.M)
    if len(body) > REPLY_LIMIT:
        body = body[:REPLY_LIMIT - 40].rstrip() + "\n\nдальше обрезал под лимит Телеграма"
    return body


def run_agent(task, name=None, path=None, timeout=TIMEOUT, log=None, trust=None):
    """Один запуск: промпт, процесс, разбор, чистка. Возвращает словарь ответа."""
    log = log or (lambda line: None)
    found = find_clis()
    if name is None:
        name, path = pick(found, profile_agent())
    if not name:
        return {"ok": False, "agent": "", "text": "",
                "why": "ни одного CLI не найдено: поставь claude, codex, agy или gemini"}
    edit = allow_edit(task.get("text", ""), trust)
    prompt = build_prompt(task)
    cwd = task.get("cwd") or ROOT
    out_file = ""
    if name == "codex":
        fd, out_file = tempfile.mkstemp(prefix="viora-codex-", suffix=".txt")
        os.close(fd)
    argv = build_argv(name, prompt, task, edit=edit, out_file=out_file)
    if path:
        argv[0] = path
    started = time.time()
    code, out, err = RUNNER(argv, cwd, timeout)
    text, failure = parse_output(name, out, out_file)
    if not text and not failure and code == 0 and name == "agy":
        log("agy молчит без TTY, повтор через pty")
        code, out, err = PTY_RUNNER(argv, cwd, timeout)
        text, failure = parse_output(name, out, out_file)
    if out_file:
        try:
            os.unlink(out_file)
        except OSError:
            pass
    spent = round(time.time() - started, 1)
    log("%s код %s за %s с%s" % (name, code, spent, (": " + err.strip()[:300]) if err.strip() else ""))
    if code == 124:
        return {"ok": False, "agent": name, "text": "", "why": "агент %s не уложился в %d с" % (name, timeout)}
    if not text:
        why = failure or (err.strip().split("\n")[-1][:200] if err.strip() else "пустой ответ")
        return {"ok": False, "agent": name, "text": "", "why": "%s: %s" % (name, why)}
    return {"ok": True, "agent": name, "text": clean_reply(text), "edit": edit,
            "seconds": spent, "code": code}


# ------------------------------------------------------------------ команды

def load_task(args):
    if getattr(args, "task", ""):
        try:
            with open(args.task, encoding="utf-8") as handle:
                task = json.load(handle)
        except (OSError, ValueError) as err:
            print("файл задачи не читается: %s" % err)
            return None
        if not isinstance(task, dict):
            print("файл задачи должен быть объектом JSON")
            return None
    else:
        task = {"text": getattr(args, "text", "") or ""}
    task.setdefault("text", "")
    task.setdefault("media", [])
    task["kind"] = (getattr(args, "kind", "") or task.get("kind") or "talk")
    if task["kind"] not in KINDS:
        task["kind"] = "talk"
    task.setdefault("cwd", ROOT)
    return task


def cmd_which(args):
    found = find_clis()
    prefer = profile_agent()
    name, path = pick(found, prefer)
    if getattr(args, "short", False):
        print(("%s (%s)" % (name, path)) if name else "нет ни одного CLI агента")
        return 0 if name else 1
    print("Агенты в PATH:")
    if not found:
        print("  нет ни одного: claude, codex, agy, gemini")
    for row_name, row_path in found:
        mark = "первый" if row_name == name else ""
        print("  %-7s %s %s" % (row_name, row_path, mark))
    print("Предпочтение из профиля: %s" % (prefer or "auto"))
    print("trust: %s" % trust_level())
    return 0 if found else 2


def cmd_run(args):
    task = load_task(args)
    if task is None:
        return 2
    if not task.get("text") and not task.get("media"):
        print("пустая задача: дай --text или файл задачи с полем text")
        return 2
    lines = []
    result = run_agent(task, timeout=int(getattr(args, "timeout", TIMEOUT) or TIMEOUT),
                       log=lines.append)
    for line in lines:
        sys.stderr.write(line + "\n")
    if getattr(args, "out", ""):
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write((result.get("text") or "") + "\n")
    if getattr(args, "as_json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("ok"):
        print(result["text"])
    else:
        print(result.get("why") or "агент не ответил")
    if result.get("ok"):
        return 0
    return 2 if not result.get("agent") else 1


# ------------------------------------------------------------------ тесты

def selftest():
    global RUNNER, PTY_RUNNER, WHICH
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    task = {"text": "реши: сколько будет 2+2", "media": ["/tmp/a.jpg"], "kind": "solve",
            "cwd": "/work"}
    prompt = build_prompt(task)
    ok("промпт зовёт читать AGENTS.md и SKILL.md", "AGENTS.md" in prompt and "SKILL.md" in prompt)
    ok("промпт запрещает secrets", "secrets/" in prompt)
    ok("промпт несёт задачу автора", "2+2" in prompt)
    ok("промпт несёт путь к картинке", "/tmp/a.jpg" in prompt)
    ok("промпт просит без тире", "Без длинных тире" in prompt)
    ok("промпт ограничивает длину", str(REPLY_LIMIT) in prompt)
    ok("в промпте нет секретов", "TELEGRAM_" not in prompt and "api_hash" not in prompt)
    ok("пост идёт по пайплайну", "ship" in build_prompt(dict(task, kind="request")))
    ok("разговор без пайплайна", "ship" not in build_prompt(dict(task, kind="talk")))

    argv = build_argv("claude", "p", task)
    ok("claude: -p и json", argv[:3] == ["claude", "-p", "p"] and "json" in argv)
    ok("claude: только чтение", READ_TOOLS in argv and "Edit" not in " ".join(argv))
    argv = build_argv("claude", "p", task, edit=True)
    ok("claude: правка по разрешению", "Edit" in " ".join(argv))
    argv = build_argv("codex", "p", task, out_file="/tmp/o.txt")
    ok("codex: exec и read-only", argv[1] == "exec" and "read-only" in argv)
    ok("codex: картинка до разделителя, промпт после",
       argv.index("-i") < argv.index("--") and argv[-1] == "p" and argv[-2] == "--")
    ok("codex: корень через -C", argv[argv.index("-C") + 1] == "/work")
    ok("codex: файл ответа", "--output-last-message" in argv)
    argv = build_argv("codex", "p", task, edit=True)
    ok("codex: правка открывает workspace-write", "workspace-write" in argv)
    ok("agy: -p и json", build_argv("agy", "p", task)[:3] == ["agy", "-p", "p"])
    ok("gemini: -p и json", build_argv("gemini", "p", task)[:3] == ["gemini", "-p", "p"])

    ok("claude json разбирается", parse_output("claude", '{"type":"result","result":"четыре"}')[0] == "четыре")
    ok("claude ошибка даёт пусто и причину",
       parse_output("claude", '{"is_error":true,"result":"Not logged in"}') == ("", "Not logged in"))
    ok("предупреждение перед json не мешает",
       parse_output("claude", 'Warning: no stdin\n{"result":"ок"}')[0] == "ок")
    ok("gemini response разбирается", parse_output("gemini", '{"response":"пять"}')[0] == "пять")
    ok("agy пустой stdout даёт пусто", parse_output("agy", "") == ("", ""))
    ok("голый текст остаётся текстом", parse_output("agy", "просто текст")[0] == "просто текст")
    jsonl = ('{"type":"item.completed","item":{"item_type":"reasoning","text":"думаю"}}\n'
             '{"type":"item.completed","item":{"item_type":"assistant_message","text":"шесть"}}\n')
    ok("codex jsonl даёт последний ответ", parse_output("codex", jsonl)[0] == "шесть")
    failed = '{"type":"turn.failed","error":{"message":"quota exceeded"}}\n'
    ok("codex turn.failed даёт причину", parse_output("codex", failed) == ("", "quota exceeded"))
    tmp = tempfile.mkdtemp(prefix="viora-agent-")
    try:
        out_file = os.path.join(tmp, "last.txt")
        with open(out_file, "w", encoding="utf-8") as handle:
            handle.write("семь\n")
        ok("codex файл ответа важнее stdout", parse_output("codex", "мусор", out_file)[0] == "семь")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok("тире вычищаются", "\u2014" not in clean_reply("десять \u2014 двадцать"))
    ok("заголовки markdown снимаются", not clean_reply("## Ответ\nтекст").startswith("#"))
    ok("длинный ответ режется", len(clean_reply("я" * 5000)) <= REPLY_LIMIT)

    ok("поправь файл это просьба о правке", wants_edit("Виора, поправь файл tools/x.py"))
    ok("реши это не правка", not wants_edit("реши задачу"))
    ok("без trust=edit правки нет", not allow_edit("поправь файл", trust="read"))
    ok("с trust=edit и просьбой правка есть", allow_edit("поправь файл", trust="edit"))
    ok("с trust=edit без просьбы правки нет", not allow_edit("реши", trust="edit"))

    def which_all(name):
        return "/usr/bin/%s" % name

    def which_none(name):
        return None

    ok("нашли всех четверых", [n for n, _ in find_clis(which_all)] == list(ORDER))
    ok("без CLI список пуст", find_clis(which_none) == [])
    ok("предпочтение из профиля работает", pick(find_clis(which_all), "agy")[0] == "agy")
    ok("auto берёт первого", pick(find_clis(which_all), "auto")[0] == "claude")
    ok("неизвестное предпочтение падает на первого", pick(find_clis(which_all), "grok")[0] == "claude")

    keep = (RUNNER, PTY_RUNNER, WHICH)
    calls = []

    def fake_runner(reply, code=0, err=""):
        def run(argv, cwd, timeout):
            calls.append(list(argv))
            return code, reply, err
        return run

    try:
        WHICH = which_all
        RUNNER = fake_runner('{"result":"ответ клода"}')
        res = run_agent(dict(task), name="claude", path="/usr/bin/claude", trust="read")
        ok("claude отвечает", res["ok"] and res["text"] == "ответ клода" and res["agent"] == "claude")
        ok("claude запущен с промптом", calls and "AGENTS.md" in calls[-1][2])

        RUNNER = fake_runner('{"type":"item.completed","item":{"item_type":"assistant_message","text":"ответ кодекса"}}')
        res = run_agent(dict(task), name="codex", path="/usr/bin/codex", trust="read")
        ok("codex отвечает из jsonl", res["ok"] and res["text"] == "ответ кодекса")
        ok("codex получил read-only", "read-only" in calls[-1])

        RUNNER = fake_runner("", code=0)
        PTY_RUNNER = fake_runner('{"response":"ответ через pty"}')
        res = run_agent(dict(task), name="agy", path="/usr/bin/agy", trust="read")
        ok("agy пустой stdout уходит в pty", res["ok"] and res["text"] == "ответ через pty")

        RUNNER = fake_runner('{"response":"ответ гемини"}')
        res = run_agent(dict(task), name="gemini", path="/usr/bin/gemini", trust="read")
        ok("gemini отвечает", res["ok"] and res["text"] == "ответ гемини")

        RUNNER = fake_runner("", code=0)
        PTY_RUNNER = fake_runner("", code=0)
        res = run_agent(dict(task), name="claude", path="/usr/bin/claude", trust="read")
        ok("пустой ответ это провал с причиной", not res["ok"] and "пустой" in res["why"])

        RUNNER = fake_runner("", code=124, err="не уложился в 600 секунд")
        res = run_agent(dict(task), name="claude", path="/usr/bin/claude", trust="read")
        ok("таймаут это провал с причиной", not res["ok"] and "не уложился" in res["why"])

        RUNNER = fake_runner("", code=1, err="Not logged in. Please run /login")
        res = run_agent(dict(task), name="claude", path="/usr/bin/claude", trust="read")
        ok("ошибка входа доезжает до причины", not res["ok"] and "login" in res["why"])

        RUNNER = fake_runner('{"is_error":true,"result":"Not logged in · Please run /login"}', code=1)
        res = run_agent(dict(task), name="claude", path="/usr/bin/claude", trust="read")
        ok("is_error в json не считается ответом", not res["ok"] and "login" in res["why"])

        WHICH = which_none
        res = run_agent(dict(task), trust="read")
        ok("без CLI честный отказ", not res["ok"] and not res["agent"] and "claude" in res["why"])

        WHICH = which_all
        RUNNER = fake_runner('{"result":"правлю"}')
        res = run_agent(dict(task, text="поправь файл tools/x.py"), name="claude",
                        path="/usr/bin/claude", trust="edit")
        ok("правка включается по слову и trust", res["ok"] and res["edit"] and "Edit" in " ".join(calls[-1]))
        res = run_agent(dict(task, text="поправь файл tools/x.py"), name="claude",
                        path="/usr/bin/claude", trust="read")
        ok("без trust правка не включается", res["ok"] and not res["edit"])
    finally:
        RUNNER, PTY_RUNNER, WHICH = keep

    bad = [name for name, good in checks if not good]
    for name, good in checks:
        print("%s %s" % ("PASS" if good else "FAIL", name))
    print("Итого: %d проверок агента, провалилось %d" % (len(checks), len(bad)))
    return 1 if bad else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        return selftest()
    ap = argparse.ArgumentParser(description="Подписочный CLI агента как мозг для Телеграма")
    ap.add_argument("--version", action="version", version="agent.py %s" % VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("which", help="какие CLI найдены и кто первый")
    p.add_argument("--short", action="store_true", help="одна строка для doctor")
    p.set_defaults(func=cmd_which)

    p = sub.add_parser("run", help="выполнить задачу")
    p.add_argument("--task", default="", help="файл задачи JSON")
    p.add_argument("--text", default="", help="текст задачи вместо файла")
    p.add_argument("--kind", default="", choices=("",) + KINDS)
    p.add_argument("--out", default="", help="куда записать ответ")
    p.add_argument("--timeout", type=int, default=TIMEOUT)
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("selftest", help="свои тесты")
    p.set_defaults(func=lambda a: selftest())

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
