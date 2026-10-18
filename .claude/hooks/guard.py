#!/usr/bin/env python3
"""Защитный хук Claude Code (PreToolUse): не даёт прочитать пароли и испортить почту.

Правило в тексте инструкции Claude может забыть к концу длинной сессии, хук срабатывает всегда.
Получает JSON события на stdin; код выхода 2 блокирует действие, текст из stderr Claude увидит.
Блокирует:
  * чтение, поиск и правку config.env / .env / identities.json (пароли ящиков): их вписывает человек сам, например через nano;
  * вывод всех переменных окружения (printenv, env, export, set, /proc/*/environ);
  * команды, которые портят или стирают почту: EXPUNGE, пометка \\Deleted, imapsync --delete, doveadm expunge/delete/purge,
    rm и find -delete по каталогам почты;
  * добавление файла с паролями в git.
Правку самой защиты (.claude/settings.json, .claude/hooks/) не запрещает, а просит подтверждение человека.
Это страховка от случайности, а не от целенаправленного обхода: имя файла, собранное из частей, она не поймает.
Правила можно менять под себя; после правок запусти python3 tests/test_guard.py.
"""
import json
import os
import re
import shlex
import sys

SECRET_FILE = re.compile(r"config\.env\b|(^|/)\.env\b|identities\.json\b")
READERS = {"cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "bat", "strings",
           "xxd", "od", "cut", "sort", "source", ".", "cp", "mv", "base64", "curl", "wget", "diff", "cmp", "install",
           "paste", "nl", "tac", "rev", "uniq", "fold", "column", "zcat", "scp", "rsync", "tar", "zip", "gzip", "split"}
SEARCHERS = {"grep", "egrep", "fgrep", "rg", "ag"}   # первый аргумент без «-» у них — шаблон, не файл
COPIERS = {"cp", "mv", "install", "scp", "rsync"}    # последний аргумент — назначение; запись туда секрета не читает его
MAIL_PATH = re.compile(r"vmail|[Mm]aildir|/var/mail\b|/mail(/|$)")

DANGEROUS = [
    (re.compile(r"\bEXPUNGE\b|\.expunge\(", 0), "команда EXPUNGE стирает письма навсегда"),
    (re.compile(r"\+?FLAGS[^\n]*\\+Deleted", re.I), "пометка \\Deleted готовит письма к удалению"),
    (re.compile(r"imapsync[^\n;|&]*--delete", re.I), "imapsync с --delete удаляет письма"),
    (re.compile(r"doveadm\s[^\n;|&]*\b(expunge|delete|purge|force-resync|deduplicate)\b", re.I), "doveadm может удалить письма"),
    (re.compile(r"rsync\b[^\n;|&]*--delete[^\n;|&]*(vmail|[Mm]aildir)", re.I), "rsync --delete по каталогу почты"),
    (re.compile(r"/proc/[^\s/]+/environ"), "переменные окружения процесса могут содержать пароли"),
    (re.compile(r"<\s*['\"]?[^\s;|&]*config\.env"), "нельзя читать файл с паролями (config.env)"),
]


def block(msg):
    sys.stderr.write("Заблокировано защитным хуком: " + msg + "\nСкажи человеку, что нужно, и пусть он сделает это сам или явно разрешит.\n")
    sys.exit(2)


def ask(msg):
    """Не блокируем, а просим подтверждение у человека."""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                               "permissionDecisionReason": msg}}, ensure_ascii=False))
    sys.exit(0)


def words(part):
    try:
        return shlex.split(part)
    except ValueError:
        return part.split()


def check_part(part):
    ws = words(part)
    if not ws:
        return
    # переменные в начале («A=1 cmd») и sudo/env/nohup не считаем командой
    i = 0
    while i < len(ws) and (re.match(r"^[A-Za-z_]\w*=", ws[i]) or ws[i] in ("sudo", "nohup", "time", "command")):
        i += 1
    ws = ws[i:]
    if not ws:
        return
    cmd = os.path.basename(ws[0])
    args = ws[1:]
    if re.fullmatch(r"python\d*(\.\d+)?|perl|ruby|node|php", cmd) and any(a in ("-c", "-e", "-E", "-r") for a in args) \
            and any(SECRET_FILE.search(a) for a in args):
        block("нельзя открывать файл с паролями (config.env) через интерпретатор")
    if cmd in READERS:
        files = [a for a in args if not a.startswith("-")]
        if cmd in SEARCHERS and files:
            files = files[1:]                    # первый — шаблон поиска
        if cmd in COPIERS and len(files) > 1:
            files = files[:-1]                   # последний — куда копируем
        elif cmd in COPIERS:
            files = []
        if any(SECRET_FILE.search(f) for f in files):
            block("нельзя читать и копировать файл с паролями (config.env); скрипты читают его сами")
    if cmd == "git" and args and args[0] in ("add", "stage"):
        if any(SECRET_FILE.search(a) or re.fullmatch(r"(\./)?(config\.\*|\.env\*?|\*\.env|\*\.json)", a) for a in args[1:]):
            block("файл с паролями нельзя добавлять в git")
    if cmd == "rm" and any(MAIL_PATH.search(a) for a in args):
        block("удаление файлов в каталоге с почтой")
    if cmd == "find" and "-delete" in args and any(MAIL_PATH.search(a) for a in args):
        block("find -delete по каталогу с почтой")
    if cmd == "truncate" and any(MAIL_PATH.search(a) for a in args):
        block("обрезка файлов в каталоге с почтой")
    if cmd in ("printenv", "export", "set", "env") and not [a for a in args if not a.startswith("-") and "=" not in a]:
        if cmd == "env" and any("=" in a for a in args):
            return
        block("вывод всех переменных окружения может показать пароли")
    if cmd == "declare" and "-p" in args:
        block("вывод переменных окружения может показать пароли")


def check_bash(cmd):
    for rx, why in DANGEROUS:
        if rx.search(cmd):
            block(why)
    for part in re.split(r"[;&|\n]+|\$\(|`", cmd):
        check_part(part.strip().strip("()"))


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        sys.exit(0)   # не понял событие: не мешаем работе
    tool, inp = ev.get("tool_name", ""), ev.get("tool_input", {}) or {}
    if tool == "Bash":
        check_bash(str(inp.get("command", "")))
        return
    paths = [str(inp.get(k, "")) for k in ("file_path", "notebook_path", "path", "glob")]
    if tool in ("Grep", "Glob"):
        paths.append(str(inp.get("pattern", "")) if tool == "Glob" else "")
    if any(SECRET_FILE.search(p) for p in paths if p):
        block("файл с паролями (config.env) человек заполняет и читает сам; не открывай, не ищи в нём и не меняй")
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit") and \
            any(re.search(r"\.claude/(settings(\.local)?\.json|hooks/)", p) for p in paths):
        ask("правка защитных настроек Claude Code: подтверди, что это нужно")


if __name__ == "__main__":
    main()
