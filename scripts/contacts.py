#!/usr/bin/env python3
"""Заполняет адресную книгу Roundcube подсказками из переписки (как «Автодополнение адресов» у Яндекса).
Для каждого ящика: адреса и имена из заголовков From/To/Cc/Reply-To всех папок + справочник сотрудников.
Пользователь Roundcube и профиль (имя в «От») создаются, если их ещё нет. Повторный запуск безопасен:
прежние автоматические контакты (метка X-AUTO-CONTACT) пересоздаются, ручные не трогаются.
Требует Roundcube на MySQL/MariaDB и команду `mysql` на сервере (под root без пароля через сокет).
Настройки: config.env (DST_HOST, DOMAIN, RC_DB, пароли MAIL_NEW_<ЛОГИН>_PASS) и identities.json."""
import collections
import email
import email.header
import email.utils
import imaplib
import re
import ssl
import subprocess
import sys

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.environ.get("MAIL_ENV_FILE", os.path.join(HERE, "..", "config.env"))

def load_identities():
    """identities.json: {"identities": {"info": "Имя в поле От"}, "staff": [["boss@example.ru", "Директор"]]}"""
    d = json.load(open(os.path.join(HERE, "..", "identities.json"), encoding="utf-8"))
    return d["identities"], [tuple(x) for x in d.get("staff", [])]


JUNK = re.compile(r"(no-?reply|noreply|donotreply|do-not-reply|mailer-daemon|postmaster|bounce|notification|unsubscribe|"
                  r"@.*\.(?:mail|email)\.sender|rassilka|newsletter|robot@)", re.I)
SKIP_FOLDERS = {"Trash", "Spam", "Junk"}


def parse_env_line(line):
    """Строка config.env -> (ключ, значение) или None. Понимает пробелы вокруг «=», кавычки и хвостовой комментарий « # ...»."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, v = line.split("=", 1)
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    else:
        v = re.split(r"\s+#", v, 1)[0].strip()
    return k.strip(), v


def load_env():
    d = {}
    for l in open(ENV, encoding="utf-8"):
        kv = parse_env_line(l)
        if kv:
            d[kv[0]] = kv[1]
    return d


def dec(v):
    try:
        return str(email.header.make_header(email.header.decode_header(v or ""))).strip()
    except Exception:
        return (v or "").strip()


def q(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'").replace("\x00", "") + "'"


def sql(stmt, fetch=False):
    # SQL передаём через stdin: нет лимита длины аргумента и текст переписки не виден в списке процессов
    r = subprocess.run(["mysql", "--default-character-set=utf8mb4", "-N", "-B", RCDB],
                       input=stmt, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout


def harvest(box, pw):
    cnt = collections.Counter()
    names = {}
    m = imaplib.IMAP4_SSL(HOST, PORT, ssl_context=ssl.create_default_context(), timeout=120)
    m.login(f"{box}@{DOMAIN}", pw)
    typ, lst = m.list()
    folders = []
    for raw in lst:
        mt = re.match(rb'\((?P<f>.*?)\) (?P<d>"[^"]*"|NIL) (?P<n>.+)$', raw)
        if mt and b"\\Noselect" not in mt.group("f"):
            folders.append(mt.group("n").decode().strip('"'))
    for f in folders:
        if f in SKIP_FOLDERS:
            continue
        typ, d = m.select('"%s"' % f, readonly=True)
        if typ != "OK":
            continue
        n = int(d[0] or 0)
        for i in range(1, n + 1, 400):
            typ, resp = m.fetch(f"{i}:{min(n, i + 399)}", "(BODY.PEEK[HEADER.FIELDS (FROM TO CC REPLY-TO)])")
            for x in resp:
                if not isinstance(x, tuple):
                    continue
                msg = email.message_from_bytes(x[1])
                for h in ("From", "To", "Cc", "Reply-To"):
                    for name, addr in email.utils.getaddresses([dec(msg.get(h, ""))]):
                        addr = addr.strip().lower()
                        if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", addr) or len(addr) > 80:
                            continue
                        cnt[addr] += 1
                        name = name.strip().strip("\"'").strip()
                        if name and name.lower() != addr and len(name) <= 90 and (addr not in names or len(name) > len(names[addr])):
                            names[addr] = name
    m.logout()
    return cnt, names


def words_of(name, addr):
    toks = re.findall(r"[\w]+", (name + " " + addr).lower())
    toks += re.split(r"[._\-@]+", addr.lower())
    toks += [addr.lower(), addr.lower().split("@")[0]]   # адрес целиком и его левая часть (info, info@example.ru)
    return " ".join(sorted(set(t for t in toks if t)))


def vcard(name, addr):
    name = re.sub(r"[\r\n]+", " ", name)
    parts = name.split()
    fn = parts[0] if parts else ""
    sn = " ".join(parts[1:]) if len(parts) > 1 else ""
    return ("BEGIN:VCARD\r\nVERSION:3.0\r\n" + f"FN:{name or addr}\r\nN:{sn};{fn};;;\r\n" +
            f"EMAIL;TYPE=INTERNET:{addr}\r\nX-AUTO-CONTACT:1\r\nEND:VCARD"), fn, sn


def main():
    env = load_env()
    IDENT, STAFF = load_identities()
    global HOST, PORT, DOMAIN, RCDB, MAILHOST
    HOST, PORT, DOMAIN, RCDB = env["DST_HOST"], int(env.get("DST_PORT", "993")), env["DOMAIN"], env.get("RC_DB", "roundcube")
    MAILHOST = env.get("RC_MAIL_HOST", "localhost")   # должен совпадать с imap_host в конфиге Roundcube
    only = sys.argv[1:]
    for box, ident in IDENT.items():
        if only and box not in only:
            continue
        key = "MAIL_NEW_" + box.upper().replace("-", "_") + "_PASS"
        if key not in env:
            print(f"{box}: нет пароля {key}, пропуск")
            continue
        me = f"{box}@{DOMAIN}"
        cnt, names = harvest(box, env[key])
        for addr, nm in STAFF:
            if addr != me:
                cnt[addr] += 10 ** 6
                names[addr] = nm
        cnt.pop(me, None)
        ranked = [a for a, c in cnt.most_common() if not JUNK.search(a) and (c > 1 or a in names)][:5000]
        # пользователь и профиль
        uid = sql(f"select user_id from users where username={q(me)}").strip()
        if not uid:
            sql(f"insert into users (username, mail_host, created, language) values ({q(me)}, {q(MAILHOST)}, now(), 'ru_RU')")
            uid = sql(f"select user_id from users where username={q(me)}").strip()
        if not uid.isdigit():
            raise RuntimeError(f"не удалось получить user_id для {me}: {uid!r}")
        sql(f"update identities set name={q(ident)} where user_id={uid} and standard=1 and (name='' or name is null)")
        if not sql(f"select identity_id from identities where user_id={uid} limit 1").strip():
            sql(f"insert into identities (user_id, changed, del, standard, name, organization, email, `reply-to`, bcc, html_signature) "
                f"values ({uid}, now(), 0, 1, {q(ident)}, '', {q(me)}, '', '', 0)")
        # контакты
        have = set(x.lower() for x in sql(f"select email from contacts where user_id={uid} and del=0 and vcard not like '%X-AUTO-CONTACT%'").split())
        rows = []
        for a in ranked:
            if a in have:
                continue
            nm = names.get(a, "")
            vc, fn, sn = vcard(nm, a)
            rows.append(f"({q(nm or a)}, {q(a)}, {q(fn)}, {q(sn)}, {q(vc)}, {q(words_of(nm, a))}, {uid}, 0, now())")
        # удаление старых автоконтактов и вставка новых — одной транзакцией: сбой посередине не оставит книгу пустой
        stmts = ["START TRANSACTION;", f"delete from contacts where user_id={uid} and vcard like '%X-AUTO-CONTACT%';"]
        for i in range(0, len(rows), 300):
            stmts.append("insert into contacts (name, email, firstname, surname, vcard, words, user_id, del, changed) values "
                         + ",".join(rows[i:i + 300]) + ";")
        stmts.append("COMMIT;")
        sql("\n".join(stmts))
        print(f"{box}: контактов добавлено {len(rows)} (из {len(cnt)} адресов в переписке); пользователь {uid}, имя в «От»: {ident}")


if __name__ == "__main__":
    main()
