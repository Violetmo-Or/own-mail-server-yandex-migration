#!/usr/bin/env python3
"""Поиск по всем ящикам сразу (SQLite FTS5), запускается НА ПОЧТОВОМ СЕРВЕРЕ.
  search.py                       — дополнить индекс новыми письмами (повторно безопасно, ставьте в cron/таймер)
  search.py search "слова" [-m ящик] [-n 20] [--from X] [--since 2025-01-01] [--subject] [--dedup]
Ищет по отправителю, получателям, теме, тексту и именам вложений; находит по началу слова (пред* найдёт «предложение»).
Читает только файлы maildir, ничего не меняет в почте.
Настройки в config.env: MAILDIR_BASE (каталог с папками ящиков), MAILDIR_SUBDIR (по умолчанию Maildir), SEARCH_DB."""
import argparse, email, email.header, email.policy, email.utils, os, re, sqlite3, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))


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


def _cfg(name, default):
    if name in os.environ:
        return os.environ[name]
    try:
        for line in open(os.environ.get("MAIL_ENV_FILE", os.path.join(HERE, "..", "config.env")), encoding="utf-8"):
            kv = parse_env_line(line)
            if kv and kv[0] == name:
                return kv[1]
    except OSError:
        pass
    return default


BASE = _cfg("MAILDIR_BASE", "/var/vmail")          # внутри: по папке на ящик (имя папки = имя ящика)
SUBDIR = _cfg("MAILDIR_SUBDIR", "Maildir")          # где у ящика лежит maildir (у Dovecot обычно Maildir)
DB = _cfg("SEARCH_DB", os.path.join(HERE, "..", "mail-search.db"))
BODY_CAP = 60000


def dec(v):
    try:
        return str(email.header.make_header(email.header.decode_header(v or ""))).strip()
    except Exception:
        return (v or "").strip()


def body_of(msg):
    out, att = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn:
            att.append(dec(fn))
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html"):
            try:
                t = part.get_payload(decode=True) or b""
                t = t.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ct == "text/html":
                t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
                t = re.sub(r"<[^>]+>", " ", t)
            out.append(t)
        elif part.get("X-Original-Filename"):
            att.append(dec(part.get("X-Original-Filename")))
    return re.sub(r"\s+", " ", " ".join(out))[:BODY_CAP], " ".join(att)


def connect():
    os.umask(0o077)   # индекс содержит текст всей переписки: только владелец
    db = sqlite3.connect(DB)
    try:
        os.chmod(DB, 0o600)
    except OSError:
        pass
    db.executescript("""
    create table if not exists seen(path text primary key);
    create table if not exists meta(id integer primary key, mailbox text, folder text, date text, path text);
    create virtual table if not exists mail using fts5(sender, recipients, subject, body, attachments,
        tokenize='unicode61 remove_diacritics 2', prefix='2 3 4');
    """)
    return db


def index():
    db = connect()
    t0, new, bad = time.time(), 0, 0
    if not os.path.isdir(BASE):
        sys.exit(f"MAILDIR_BASE не существует: {BASE}. Поправьте путь в config.env.")
    boxes = [m for m in sorted(os.listdir(BASE)) if os.path.isdir(os.path.join(BASE, m, SUBDIR))]
    if not boxes:
        sys.exit(f"В {BASE} нет ящиков с каталогом {SUBDIR}. Проверьте MAILDIR_BASE и MAILDIR_SUBDIR в config.env "
                 f"(у Dovecot бывает /var/vmail/<домен>/<пользователь>/Maildir).")
    for mb in boxes:
        root = os.path.join(BASE, mb, SUBDIR)
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            if os.path.basename(dirpath) not in ("cur", "new"):
                continue
            folder = os.path.basename(os.path.dirname(dirpath)).lstrip(".")
            folder = "INBOX" if folder.lower() in ("", "maildir", SUBDIR.lower()) else folder
            for f in files:
                p = os.path.join(dirpath, f)
                key = mb + "/" + folder + "/" + f.split(":")[0]
                if db.execute("select 1 from seen where path=?", (key,)).fetchone():
                    continue
                try:
                    msg = email.message_from_bytes(open(p, "rb").read(), policy=email.policy.compat32)
                    body, att = body_of(msg)
                    try:
                        d = email.utils.parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
                        ds = d.strftime("%Y-%m-%d") if d else ""
                    except Exception:
                        ds = ""   # битая дата (часто в спаме): письмо всё равно индексируем
                    rcpt = " ".join(dec(msg.get(h, "")) for h in ("To", "Cc"))
                    cur = db.execute("insert into mail(sender,recipients,subject,body,attachments) values(?,?,?,?,?)",
                                     (dec(msg.get("From")), rcpt, dec(msg.get("Subject")), body, att))
                    db.execute("insert into meta values(?,?,?,?,?)", (cur.lastrowid, mb, folder, ds, p))
                    new += 1
                except Exception:
                    bad += 1
                db.execute("insert or ignore into seen values(?)", (key,))
                if new and new % 2000 == 0 and not bad:
                    db.commit()
                    print(f"  проиндексировано {new}, {time.time()-t0:.0f} с", flush=True)
    db.commit()
    total = db.execute("select count(*) from meta").fetchone()[0]
    print(f"добавлено {new}, ошибок разбора {bad}, всего в индексе {total}, {time.time()-t0:.0f} с")


def search(a):
    db = connect()
    words = re.findall(r"\w+", a.query)
    if a.subject:                     # номер (заявки, договора) ищем точно: 1125 не должно находить 11250
        q = "subject: (" + " ".join(f'"{w}"' for w in words) + ")"
    else:
        q = " ".join(f'"{w}"*' for w in words)
    if not words:
        sys.exit("пустой запрос")
    sql = ("select m.mailbox, m.folder, m.date, f.sender, f.subject, snippet(mail,3,'[',']','…',12), f.recipients "
           "from mail f join meta m on m.id=f.rowid where mail match ?")
    args = [q]
    if a.m:
        sql += " and m.mailbox=?"; args.append(a.m)
    if a.since:
        sql += " and m.date>=?"; args.append(a.since)
    if a.sender:
        sql += " and f.sender like ?"; args.append(f"%{a.sender}%")
    sql += " order by m.date desc limit ?"
    args.append(a.n * 4 if a.dedup else a.n)
    rows = db.execute(sql, args).fetchall()
    if a.dedup:                       # одно и то же письмо лежит в нескольких папках/копиях черновиков — показываем один раз
        seen, uniq = set(), []
        for r in rows:
            k = (r[0], r[2], r[3], r[4], r[6])
            if k not in seen:
                seen.add(k); uniq.append(r)
        rows = uniq[:a.n]
    for mb, fo, d, s, sub, sn, rc in rows:
        fo = "INBOX" if fo.lower() in ("maildir", SUBDIR.lower()) else fo
        to = f" → {rc[:50]}" if a.dedup and re.search(r"(?i)sent|draft|отправ|чернов", fo) and rc else ""
        print(f"{d} | {mb} / {fo} | {s[:40]}{to} | {sub[:70]}\n    {sn}")
    print(f"найдено показано: {len(rows)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        ap = argparse.ArgumentParser()
        ap.add_argument("cmd"); ap.add_argument("query"); ap.add_argument("-m"); ap.add_argument("-n", type=int, default=20)
        ap.add_argument("--from", dest="sender"); ap.add_argument("--since")
        ap.add_argument("--subject", action="store_true", help="искать только в теме, точное слово (номер заявки)")
        ap.add_argument("--dedup", action="store_true", help="убрать повторы одного письма")
        search(ap.parse_args())
    else:
        index()
