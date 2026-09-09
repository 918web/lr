#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install.py 1.0.0

Ставит скилл VIORA STUDIO в каталоги обнаружения агентов.
Без сторонних библиотек, только стандартная поставка питона.

Куда ставит в проекте:
  .claude/skills/viora-studio    Claude Code
  .agents/skills/viora-studio    спека Agent Skills, Codex и прочие
  .cursor/skills/viora-studio    Cursor

Куда ставит с флагом --global:
  ~/.claude/skills/viora-studio
  ~/.agents/skills/viora-studio
  ~/.cursor/skills/viora-studio
  ~/.gemini/config/skills/viora-studio    Antigravity

Как гонять:
  python3 tools/install.py --target /путь/к/проекту
  python3 tools/install.py --target /путь/к/проекту --copy
  python3 tools/install.py --global
  python3 tools/install.py --target . --dry-run
  python3 tools/install.py --target . --uninstall
  python3 tools/install.py --selftest

Три режима, и главное про них вот что.

  stub  по умолчанию. В папку обнаружения ложится один SKILL.md с указателем на
        канонический скилл. Память канала, профиль автора и напоминания остаются
        в одном месте, поэтому Claude Code, Codex, Cursor и Antigravity видят
        одно и то же. Четыре полные копии скилла означали четыре разные памяти.
  copy  флаг --copy. Полная копия скилла тем же составом, что и раньше. Нужна
        только там, где агент не может дойти до канонической папки по пути.
  link  флаг --link. Симлинк. Живёт на Linux и macOS, но проводник Windows и
        7-Zip такие ссылки ломают, поэтому в поставке симлинков нет.

С флагом --global указатель в заглушке абсолютный: домашняя папка и проект лежат
в разных местах, относительный путь оттуда не дойдёт.
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import sys
import tempfile

VERSION = "1.0.0"

SKILL_NAME = "viora-studio"
TARGET_DIRS = (
    os.path.join(".claude", "skills"),
    os.path.join(".agents", "skills"),
    os.path.join(".cursor", "skills"),
)
# Antigravity в домашней папке смотрит свой каталог, в проекте ему хватает .agents.
GLOBAL_DIRS = TARGET_DIRS + (os.path.join(".gemini", "config", "skills"),)
MARKER = ".viora-install.json"
MODES = ("stub", "copy", "link")

# По этой метке заглушку узнают и сборщик, и валидатор, и человек глазами.
STUB_RE = re.compile(r'<!--\s*viora-stub target="([^"]*)"\s*-->')

# Запас на случай, если SKILL.md рядом без шапки: слова-триггеры терять нельзя.
FALLBACK_DESC = (
    "Пишет посты, статьи и сценарии для Telegram-канала VIORA STUDIO голосом badVIno. "
    "Бери этот скилл, когда в задаче есть слова: пост, телега, телеграм, канал, viora, "
    "виора, абуз, сервисы, гайд, релиз, девлог, шортс, лидмагнит, неробит, опрос, "
    "телеграф, статья, анонс, ресерч, видео, крючок, пак, подборка, оцени пост."
)

# Что обязательно должно оказаться в полной копии.
REQUIRED = (
    "SKILL.md",
    "QUICKCARD.txt",
    "ROUTER.md",
    "VERSION",
    os.path.join("reference", "formats.md"),
    os.path.join("memory", "metrics.md"),
    os.path.join("tools", "lint_post.py"),
    os.path.join("tools", "viora.py"),
    os.path.join("viora-research", "SKILL.md"),
    os.path.join("viora-video", "SKILL.md"),
)


def die(msg):
    sys.stderr.write("ОШИБКА: %s\n" % msg)
    sys.exit(1)


def skill_root():
    """Папка скилла: та самая, где лежит SKILL.md рядом с tools/."""
    env = os.environ.get("VIORA_SKILL_DIR")
    if env:
        root = os.path.abspath(env)
    else:
        root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    if not os.path.exists(os.path.join(root, "SKILL.md")):
        die("не вижу SKILL.md в %s. Задай VIORA_SKILL_DIR" % root)
    return root


def read_patterns(root):
    """Шаблоны из .skillignore самого скилла."""
    path = os.path.join(root, ".skillignore")
    out = ["__pycache__/", "*.pyc", ".DS_Store"]
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def is_ignored(rel, patterns):
    rel = rel.replace(os.sep, "/")
    parts = rel.split("/")
    for pat in patterns:
        p = pat.rstrip("/")
        if pat.endswith("/"):
            if p in parts:
                return True
            if rel.startswith(p + "/"):
                return True
            continue
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(parts[-1], p):
            return True
        if rel.startswith(p + "/"):
            return True
    return False


def collect(root, patterns, full):
    """Список относительных путей к копированию."""
    files = []
    for base, dirs, names in os.walk(root):
        rel_base = os.path.relpath(base, root)
        if rel_base == ".":
            rel_base = ""
        dirs.sort()
        keep = []
        for d in dirs:
            rel = os.path.join(rel_base, d) if rel_base else d
            if full or not is_ignored(rel + "/", patterns):
                keep.append(d)
        dirs[:] = keep
        for n in sorted(names):
            rel = os.path.join(rel_base, n) if rel_base else n
            if not full and is_ignored(rel, patterns):
                continue
            files.append(rel)
    return files


def version_of(root):
    path = os.path.join(root, "VERSION")
    if os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    return "0.0.0"


def frontmatter_of(root):
    """Плоские поля шапки SKILL.md плюс metadata.version. Без сторонних yaml."""
    path = os.path.join(root, "SKILL.md")
    out = {}
    if not os.path.exists(path):
        return out
    text = open(path, encoding="utf-8", errors="replace").read()
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    for line in text[3:end].split("\n"):
        top = re.match(r"^([A-Za-z0-9_-]+):\s*(.+)$", line)
        if top:
            out[top.group(1)] = top.group(2).strip()
            continue
        nested = re.match(r"^\s+version:\s*(.+)$", line)
        if nested:
            out["version"] = nested.group(1).strip()
    return out


def stub_fields(root):
    """Шапка заглушки один в один с канонической, чтобы триггеры не разъехались."""
    fm = frontmatter_of(root)
    return {
        "name": fm.get("name") or SKILL_NAME,
        "description": fm.get("description") or FALLBACK_DESC,
        "license": fm.get("license") or "MIT",
        "version": fm.get("version") or version_of(root),
    }


def stub_text(root, target):
    """Текст заглушки для папки обнаружения. Тело держим в 15-25 строк."""
    fields = stub_fields(root)
    pointer = str(target).replace(os.sep, "/")
    rows = [
        "---",
        "name: %s" % fields["name"],
        "description: %s" % fields["description"],
        "license: %s" % fields["license"],
        "metadata:",
        "  version: %s" % fields["version"],
        "---",
        "",
        "# VIORA STUDIO: здесь только указатель",
        "",
        '<!-- viora-stub target="%s" -->' % pointer,
        "",
        "Это не сам скилл, а заглушка папки обнаружения. Настоящий скилл лежит в",
        "`%s`, открой там SKILL.md и работай по нему." % pointer,
        "",
        "Порядок такой:",
        "",
        "1. Перейди в каноническую папку: cd %s" % pointer,
        "2. Сильная модель читает SKILL.md, слабая и быстрая читает QUICKCARD.txt.",
        '3. Все команды запускай оттуда: python3 tools/viora.py route "задача словами"',
        "4. На Windows питон зовут иначе: python или py -3 вместо python3.",
        "5. Первым делом спроси профиль: python3 tools/memory.py profile missing",
        "",
        "Здесь ничего не правь. Эту папку перезаписывает tools/install.py, любая правка",
        "тут потеряется молча. Память канала, профиль автора и напоминания живут только",
        "в канонической папке, поэтому Claude Code, Codex, Cursor и Antigravity видят",
        "одно и то же.",
        "",
        "Заглушка стоит вместо копии не ради экономии места. Четыре полные копии скилла",
        "означали четыре разные памяти и четыре разных профиля автора.",
        "",
    ]
    return "\n".join(rows)


def stub_body_lines(text):
    """Строки тела заглушки без шапки: по ним видно, что тело не разбухло."""
    if not text.startswith("---"):
        return text.split("\n")
    end = text.find("\n---", 3)
    if end == -1:
        return text.split("\n")
    body = text[end + 4:].strip("\n")
    return [line for line in body.split("\n")]


def stub_target(dest):
    """Куда указывает заглушка. None значит это не заглушка."""
    path = os.path.join(dest, "SKILL.md")
    if not os.path.exists(path):
        return None
    found = STUB_RE.search(open(path, encoding="utf-8", errors="replace").read())
    return found.group(1) if found else None


def targets_for(base, dirs=None):
    return [os.path.join(base, d, SKILL_NAME) for d in (dirs or TARGET_DIRS)]


def inside(base, root):
    """Лежит ли канонический скилл внутри того же корня, куда ставим."""
    head = os.path.abspath(base).rstrip(os.sep) + os.sep
    return os.path.abspath(root).startswith(head)


def pointer_for(root, dest, absolute=False):
    """Путь от папки обнаружения до канонического скилла, всегда прямыми слешами."""
    if absolute:
        return os.path.abspath(root).replace(os.sep, "/")
    try:
        rel = os.path.relpath(os.path.abspath(root), os.path.abspath(dest))
    except ValueError:
        return os.path.abspath(root).replace(os.sep, "/")
    return rel.replace(os.sep, "/")


def ours(dest):
    """Наша ли это установка: есть маркер или имя скилла в SKILL.md."""
    if os.path.islink(dest):
        return True
    if os.path.exists(os.path.join(dest, MARKER)):
        return True
    skill = os.path.join(dest, "SKILL.md")
    if os.path.exists(skill):
        head = open(skill, encoding="utf-8", errors="replace").read(600)
        return ("name: " + SKILL_NAME) in head
    return False


def remove(dest):
    if os.path.islink(dest) or os.path.isfile(dest):
        os.unlink(dest)
    elif os.path.isdir(dest):
        shutil.rmtree(dest)


def write_marker(dest, root, mode, count, target):
    marker = {
        "skill": SKILL_NAME,
        "version": version_of(root),
        "source": root,
        "mode": mode,
        "files": count,
        "target": target,
    }
    with open(os.path.join(dest, MARKER), "w", encoding="utf-8") as fh:
        json.dump(marker, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def do_install(root, base, mode, dry, force, full, dirs=None, absolute=False):
    patterns = read_patterns(root)
    files = collect(root, patterns, full) if mode == "copy" else []
    actions = []
    for dest in targets_for(base, dirs):
        exists = os.path.islink(dest) or os.path.exists(dest)
        if exists and not force:
            if not ours(dest):
                die("в %s лежит чужое содержимое. Добавь --force, если точно надо" % dest)
        target = pointer_for(root, dest, absolute) if mode == "stub" else ""
        count = len(files) if mode == "copy" else (1 if mode == "stub" else 0)
        action = {"dest": dest, "mode": mode, "files": count,
                  "replaced": bool(exists), "target": target}
        actions.append(action)
        if dry:
            continue
        if exists:
            if not ours(dest) and not force:
                die("отказ перезаписывать %s" % dest)
            remove(dest)
        parent = os.path.dirname(dest)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        if mode == "link":
            rel = os.path.relpath(root, parent)
            os.symlink(rel, dest)
            continue
        os.makedirs(dest)
        if mode == "stub":
            with open(os.path.join(dest, "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write(stub_text(root, target))
            write_marker(dest, root, mode, 1, target)
            continue
        for rel in files:
            src = os.path.join(root, rel)
            dst = os.path.join(dest, rel)
            d = os.path.dirname(dst)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            shutil.copy2(src, dst)
        write_marker(dest, root, mode, len(files), "")
    return actions, files


def do_uninstall(base, dry, dirs=None):
    actions = []
    for dest in targets_for(base, dirs):
        if not (os.path.islink(dest) or os.path.exists(dest)):
            actions.append({"dest": dest, "removed": False, "reason": "нету"})
            continue
        if not ours(dest):
            actions.append({"dest": dest, "removed": False, "reason": "чужое"})
            continue
        if not dry:
            remove(dest)
        actions.append({"dest": dest, "removed": True, "reason": ""})
    return actions


def check_installed(dest):
    """Проверка, что установка работоспособна. У заглушки спрос один: указатель."""
    if stub_target(dest) is not None:
        return []
    if os.path.exists(os.path.join(dest, "SKILL.md")) and not os.path.isdir(
            os.path.join(dest, "tools")):
        return ["указатель на канонический скилл"]
    return [r for r in REQUIRED if not os.path.exists(os.path.join(dest, r))]


def human_mode(mode):
    return {"stub": "заглушка", "copy": "копия", "link": "симлинк"}.get(mode, mode)


def selftest():
    root = skill_root()
    fails = []

    def check(name, cond, extra=""):
        print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                           (" " + extra) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="viora-install-")
    try:
        # сухой прогон ничего не пишет
        do_install(root, tmp, "stub", True, False, False)
        check("dry-run ничего не создаёт", os.listdir(tmp) == [])

        # режим по умолчанию: заглушки
        actions, files = do_install(root, tmp, "stub", False, False, False)
        check("три каталога обнаружения", len(actions) == 3)
        dests = [a["dest"] for a in actions]
        check("все три на месте", all(os.path.isdir(d) for d in dests))
        first = dests[0]
        check("в заглушке только SKILL.md и маркер",
              sorted(os.listdir(first)) == sorted(["SKILL.md", MARKER]),
              str(sorted(os.listdir(first))))
        check("заглушка узнаётся по метке", stub_target(first) is not None)
        check("проверка установки заглушки молчит", check_installed(first) == [])

        text = open(os.path.join(first, "SKILL.md"), encoding="utf-8").read()
        fm = stub_fields(root)
        check("имя в шапке заглушки", ("name: %s" % fm["name"]) in text)
        check("описание один в один с каноническим", fm["description"] in text)
        check("слова-триггеры на месте", "виора" in text and "пост" in text)
        check("лицензия в шапке", ("license: %s" % fm["license"]) in text)
        check("версия в metadata", ("  version: %s" % fm["version"]) in text)
        body = stub_body_lines(text)
        check("тело заглушки 15-25 строк", 15 <= len(body) <= 25, str(len(body)))
        check("в заглушке нет длинных тире",
              not any(d in text for d in ("\u2014", "\u2013", "\u2015", "\u2012", "\u2212")))
        check("заглушка отправляет в каноническую папку", "здесь ничего не правь" in text.lower())

        target = stub_target(first)
        check("указатель относительный", target.startswith("../"), target)
        check("указатель ведёт на настоящий SKILL.md",
              os.path.exists(os.path.join(first, target, "SKILL.md")), target)
        check("маркер помнит указатель",
              json.load(open(os.path.join(first, MARKER), encoding="utf-8")).get("target") == target)
        check("в маркере версия скилла",
              json.load(open(os.path.join(first, MARKER), encoding="utf-8")).get("version")
              == version_of(root))
        check("заглушка не тянет файлы скилла", files == [])

        # повторная установка поверх своего же
        actions2, _ = do_install(root, tmp, "stub", False, False, False)
        check("повторная установка заменяет своё", all(a["replaced"] for a in actions2))

        # чужое содержимое не трогаем
        alien = os.path.join(tmp, ".claude", "skills", "alien-skill")
        os.makedirs(alien)
        open(os.path.join(alien, "SKILL.md"), "w", encoding="utf-8").write("---\nname: alien-skill\n---\n")
        acts = do_uninstall(tmp, False)
        check("снос убрал три заглушки", sum(1 for a in acts if a["removed"]) == 3)
        check("чужой скилл цел", os.path.exists(os.path.join(alien, "SKILL.md")))
        check("после сноса наших папок нет", not any(os.path.exists(d) for d in dests))

        # полная копия по флагу --copy
        actions3, files3 = do_install(root, tmp, "copy", False, True, False)
        missing = []
        for a in actions3:
            missing += check_installed(a["dest"])
        check("обязательные файлы скопировались", not missing, ", ".join(sorted(set(missing))))
        check("копия тянет много файлов", len(files3) > 40, str(len(files3)))
        check("кэш питона не попал",
              not any("__pycache__" in r for r in os.listdir(os.path.join(dests[0], "tools"))))
        check("тесты не попали без --full",
              not os.path.exists(os.path.join(dests[0], "tools", "tests")))
        check("у копии метки заглушки нет", stub_target(dests[0]) is None)

        # режим симлинков остался для Linux и macOS
        if hasattr(os, "symlink"):
            do_install(root, tmp, "link", False, True, False)
            check("симлинк ведёт на скилл",
                  os.path.islink(dests[0]) and os.path.exists(os.path.join(dests[0], "SKILL.md")))
            check("через симлинк видны под-скиллы",
                  os.path.exists(os.path.join(dests[0], "viora-research", "SKILL.md")))
            do_uninstall(tmp, False)
            check("симлинки сняты, источник цел",
                  not os.path.exists(dests[0]) and os.path.exists(os.path.join(root, "SKILL.md")))

        # --full тянет тесты
        do_install(root, tmp, "copy", False, True, True)
        check("--full тянет tools/tests",
              os.path.isdir(os.path.join(dests[0], "tools", "tests")))
        do_uninstall(tmp, False)

        # домашняя установка: четыре папки и абсолютный указатель
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        acts4, _ = do_install(root, home, "stub", False, False, False,
                              dirs=GLOBAL_DIRS, absolute=True)
        check("в домашней папке четыре каталога", len(acts4) == 4)
        check("среди них каталог Antigravity",
              any(os.path.join(".gemini", "config", "skills") in a["dest"] for a in acts4))
        gtarget = stub_target(acts4[0]["dest"])
        check("указатель абсолютный", os.path.isabs(gtarget), str(gtarget))
        check("абсолютный указатель ведёт на скилл",
              os.path.exists(os.path.join(gtarget, "SKILL.md")), str(gtarget))
        check("снос домашней установки",
              sum(1 for a in do_uninstall(home, False, dirs=GLOBAL_DIRS) if a["removed"]) == 4)

        # указатель считается от папки обнаружения, а не от корня проекта
        probe = os.path.join(tmp, "proj", ".claude", "skills", SKILL_NAME)
        check("относительный указатель на три уровня вверх",
              pointer_for(os.path.join(tmp, "proj", SKILL_NAME), probe)
              == "../../../" + SKILL_NAME,
              pointer_for(os.path.join(tmp, "proj", SKILL_NAME), probe))
        check("свой скилл виден внутри корня",
              inside(os.path.join(tmp, "proj"), os.path.join(tmp, "proj", SKILL_NAME)))
        check("чужой корень не считается своим",
              not inside(os.path.join(tmp, "proj"), os.path.join(tmp, "other", SKILL_NAME)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("Итого: провалилось %d" % len(fails))
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True, description="Установка скилла VIORA STUDIO")
    ap.add_argument("--target", help="корень проекта, куда ставим")
    ap.add_argument("--global", dest="glob", action="store_true", help="ставить в домашний каталог")
    ap.add_argument("--stub", action="store_true", help="заглушки с указателем (по умолчанию)")
    ap.add_argument("--copy", action="store_true", help="полная копия скилла вместо заглушки")
    ap.add_argument("--link", action="store_true", help="симлинки: только Linux и macOS")
    ap.add_argument("--full", action="store_true", help="копировать всё, включая тесты и промпты")
    ap.add_argument("--dry-run", dest="dry", action="store_true", help="только показать план")
    ap.add_argument("--force", action="store_true", help="перезаписать то, что уже есть")
    ap.add_argument("--uninstall", action="store_true", help="снять установку")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--selftest", action="store_true", help="самопроверка установщика")
    ap.add_argument("--version", action="store_true", help="версия скрипта")
    args = ap.parse_args(argv)

    if args.version:
        print("install.py %s" % VERSION)
        return 0
    if args.selftest:
        return selftest()

    picked = [name for name, on in (("stub", args.stub), ("copy", args.copy),
                                    ("link", args.link)) if on]
    if len(picked) > 1:
        die("выбери один режим: --stub, --copy или --link")
    mode = picked[0] if picked else "stub"

    root = skill_root()
    dirs = GLOBAL_DIRS if args.glob else TARGET_DIRS
    if args.glob:
        base = os.path.expanduser("~")
    elif args.target:
        base = os.path.abspath(args.target)
    else:
        die("задай --target ПУТЬ или --global")
    if not os.path.isdir(base):
        die("нет такого каталога: %s" % base)

    if args.uninstall:
        actions = do_uninstall(base, args.dry, dirs=dirs)
        if args.json:
            print(json.dumps({"action": "uninstall", "base": base, "targets": actions},
                             ensure_ascii=False, indent=2))
        else:
            for a in actions:
                mark = "снято" if a["removed"] else ("пропуск: " + a["reason"])
                print("%-52s %s" % (a["dest"], mark))
            print("Готово." if not args.dry else "Сухой прогон, ничего не тронуто.")
        return 0

    absolute = bool(args.glob) or not inside(base, root)
    actions, files = do_install(root, base, mode, args.dry, args.force, args.full,
                                dirs=dirs, absolute=absolute)
    if args.json:
        print(json.dumps({"action": "install", "base": base, "mode": mode,
                          "version": version_of(root), "files": len(files),
                          "targets": actions, "dryRun": args.dry},
                         ensure_ascii=False, indent=2))
        return 0

    print("Скилл: %s %s" % (SKILL_NAME, version_of(root)))
    print("Источник: %s" % root)
    if mode == "stub":
        print("Режим: заглушка, указатель %s" % ("абсолютный" if absolute else "относительный"))
    else:
        print("Режим: %s, файлов: %d" % (human_mode(mode), len(files)))
    for a in actions:
        state = "замена" if a["replaced"] else "новое"
        print("  %-52s %s" % (a["dest"], state))
    if args.dry:
        print("Сухой прогон: ни один файл не записан.")
        return 0
    problems = []
    for a in actions:
        problems += check_installed(a["dest"])
    if problems:
        print("Не хватает файлов: %s" % ", ".join(sorted(set(problems))))
        return 1
    if mode == "stub":
        print("Готово. Правь скилл только в %s, копий больше нет." % root)
    print("Готово. Агент увидит скилл после перезапуска сессии.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
