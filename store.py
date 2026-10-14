import os, sqlite3, time
from pathlib import Path
import json


APP_DIR = Path.home() / ".gamecredit_tool"
DB_PATH = APP_DIR / "accounts.db"
KEY_PATH = APP_DIR / "key"

try:
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except ImportError:
    _HAS_FERNET = False

def _fernet():
    if not _HAS_FERNET:
        return None
    APP_DIR.mkdir(exist_ok=True)
    if KEY_PATH.exists():
        return Fernet(KEY_PATH.read_bytes().strip())
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    try: os.chmod(KEY_PATH, 0o600)
    except OSError: pass
    return Fernet(key)

_F = _fernet()

def _enc(s: str) -> str:
    return _F.encrypt(s.encode()).decode() if (_F and s) else s
def _dec(s: str) -> str:
    if not _F or not s: return s
    try: return _F.decrypt(s.encode()).decode()
    except Exception: return s

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  qq TEXT PRIMARY KEY, nick TEXT DEFAULT '',
  gs_id TEXT NOT NULL, gs_code TEXT NOT NULL,
  exp_ts INTEGER DEFAULT 0,
  superuin TEXT DEFAULT '', supertoken TEXT DEFAULT '', superkey TEXT DEFAULT '',
  score INTEGER DEFAULT 0, score_prev INTEGER DEFAULT NULL,
  rank_v REAL DEFAULT 0, stars INTEGER DEFAULT 0,
  mtime INTEGER DEFAULT 0,
  state TEXT DEFAULT 'ok', err TEXT DEFAULT '',
  login_at INTEGER DEFAULT 0, query_at INTEGER DEFAULT 0, note TEXT DEFAULT '',
  pay_openid TEXT DEFAULT '', pay_token TEXT DEFAULT '', pay_refresh TEXT DEFAULT '',
  pay_exp_ts INTEGER DEFAULT 0, qb INTEGER DEFAULT 0, qb_prev INTEGER DEFAULT NULL,
  qb_mtime INTEGER DEFAULT 0
);"""


def conn():
    APP_DIR.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(SCHEMA)
    _migrate(c)          # ★ 新增
    c.commit()
    return c

def _migrate(c):
    """幂等加列。
       ★ 老库缺列会让 upsert 抛 OperationalError，
         而那时凭据已在内存里、进程一崩就永久丢失 —— 这个坑踩过一次。"""
    have = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
    for col, decl in (("superuin",   "TEXT DEFAULT ''"),
                      ("supertoken", "TEXT DEFAULT ''"),
                      ("superkey",   "TEXT DEFAULT ''"),
                      ("note",       "TEXT DEFAULT ''"),
                      ("rank_v",     "REAL DEFAULT 0"),
                      ("stars",      "INTEGER DEFAULT 0"),
                      ("score_prev", "INTEGER DEFAULT NULL"),
                      ("pay_openid",  "TEXT DEFAULT ''"),
                      ("pay_token",   "TEXT DEFAULT ''"),
                      ("pay_refresh", "TEXT DEFAULT ''"),
                      ("pay_exp_ts",  "INTEGER DEFAULT 0"),
                      ("qb",          "INTEGER DEFAULT 0"),
                      ("qb_prev",     "INTEGER DEFAULT NULL"),
                      ("qb_mtime",    "INTEGER DEFAULT 0")):
        if col not in have:
            c.execute(f"ALTER TABLE accounts ADD COLUMN {col} {decl}")

def upsert(r):
    try:
        _upsert_db(r)
    except Exception as e:
        _rescue(r, e)          # ★ 绝不静默丢凭据
        raise


def _upsert_db(r):
    with conn() as c:
        c.execute("""INSERT INTO accounts(qq,nick,gs_id,gs_code,exp_ts,state,login_at,
                                          superuin,supertoken,superkey)
                     VALUES(?,?,?,?,?,'ok',?,?,?,?)
                     ON CONFLICT(qq) DO UPDATE SET
                       nick=excluded.nick, gs_id=excluded.gs_id,
                       gs_code=excluded.gs_code, exp_ts=excluded.exp_ts,
                       state='ok', err='', login_at=excluded.login_at,
                       superuin=excluded.superuin, supertoken=excluded.supertoken,
                       superkey=excluded.superkey""",
                  (r.qq, r.nick, _enc(r.gs_id), _enc(r.gs_code), r.exp_ts,
                   int(time.time()), *list(getattr(r, "super_triple", ("", "", "")))))
        pay = getattr(r, "pay", None)
        if pay:
            update_pay(r.qq, pay.get("openid", ""), pay.get("access_token", ""),
                       pay.get("refresh_token", ""),
                       int(time.time()) + int(pay.get("expires_in", 5184000)),
                       pay.get("qb", 0))


def _rescue(r, err):
    """数据库写失败时的最后防线：明文落盘，人工可恢复。
       ★ 明文的代价 < 白扫一次码 + 丢 60 天凭据的代价。
         文件在用户目录，权限 600，恢复后应删除。"""
    APP_DIR.mkdir(exist_ok=True)
    p = APP_DIR / "rescue_last_login.json"
    p.write_text(json.dumps({
        "qq": r.qq, "nick": r.nick,
        "gs_id": r.gs_id, "gs_code": r.gs_code, "exp_ts": r.exp_ts,
        "super_triple": list(getattr(r, "super_triple", ("", "", ""))),
        "error": str(err), "saved_at": int(time.time()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    try: os.chmod(p, 0o600)
    except OSError: pass
    print(f"\n⚠️  数据库写入失败，凭据已紧急保存到:\n   {p}\n   原因: {err}\n"
          f"   修好数据库后运行: python -m cli rescue")


def load_rescue():
    """把 rescue 文件里的凭据导入回库"""
    p = APP_DIR / "rescue_last_login.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    from login import LoginResult
    r = LoginResult(qq=d["qq"], nick=d["nick"], gs_id=d["gs_id"],
                    gs_code=d["gs_code"], exp_ts=d["exp_ts"],
                    trace=[], super_triple=tuple(d.get("super_triple", ("", "", ""))))
    _upsert_db(r)
    p.unlink()
    return r


def all_accounts():
    with conn() as c:
        # ★ 按插入顺序（rowid）返回，新扫码的账号追加在列表末尾。
        rows = [dict(x) for x in c.execute("SELECT * FROM accounts ORDER BY rowid")]
    for x in rows:
        x["gs_id"], x["gs_code"] = _dec(x["gs_id"]), _dec(x["gs_code"])
        x["pay_openid"] = _dec(x["pay_openid"])
        x["pay_token"] = _dec(x["pay_token"])
        x["pay_refresh"] = _dec(x["pay_refresh"])
    return rows

def update_result(qq, res, state, err=""):
    with conn() as c:
        now = int(time.time())
        if getattr(res, "ok", False):
            c.execute("""UPDATE accounts SET score_prev=CASE WHEN query_at > 0
                         THEN score ELSE NULL END, score=?,rank_v=?,stars=?,mtime=?,
                         state=?,err=?,query_at=? WHERE qq=?""",
                      (res.score, res.rank, res.stars, res.mtime,
                       state, err, now, qq))
        else:
            c.execute("""UPDATE accounts SET state=?,err=?,query_at=?
                         WHERE qq=?""", (state, err, now, qq))

def delete(qq):
    with conn() as c:
        c.execute("DELETE FROM accounts WHERE qq=?", (qq,))

def update_pay(qq, openid, token, refresh, exp_ts, qb, nick=None):
    """写入 pay 侧凭据 + Q币数量。openid/token/refresh 加密落盘。

    ★ 账号若不存在（只有支付授权、还没扫码查过信用分），先建行再写入，
      否则 UPDATE 影响 0 行，新账号永远不会出现在列表里。
    ★ 昵称由调用方从扫码结果带入，避免新行昵称为空。
    """
    with conn() as c:
        now = int(time.time())
        if not c.execute("SELECT 1 FROM accounts WHERE qq=?", (qq,)).fetchone():
            c.execute("""INSERT INTO accounts(qq,nick,gs_id,gs_code,exp_ts,
                                              state,login_at)
                         VALUES(?,?,?,?,?, 'never', ?)""",
                      (qq, nick or "", "", "", 0, now))
        elif nick:
            # 已有账号昵称为空时补全，不影响已有昵称
            c.execute("UPDATE accounts SET nick=? WHERE qq=? AND (nick IS NULL OR nick='')",
                      (nick, qq))
        c.execute("""UPDATE accounts SET pay_openid=?, pay_token=?, pay_refresh=?,
                     pay_exp_ts=?, qb_prev=CASE WHEN qb_mtime > 0
                     THEN qb ELSE NULL END, qb=?, qb_mtime=? WHERE qq=?""",
                  (_enc(openid), _enc(token), _enc(refresh),
                   exp_ts, qb, now, qq))

def migrate():
    with conn() as c:
        for col in ("superuin", "supertoken", "superkey", "note"):
            try:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass          # 已存在


def import_account(qq, nick=None, gs_id=None, gs_code=None, score=None,
                   qb=None, note=None, mtime=None, query_at=None):
    """从 CSV 导入/更新一个账号。

    - qq 为唯一键；不存在则新建行，存在则就地更新。
    - gs_id/gs_code 只有 CSV 中提供时才写入，绝不覆盖已有凭据。
    - 有 gs_code 时按 JWT 计算 exp_ts，并把状态设为 ok。
    - qb 为 Q币（元），入库前 ×100 存成 Q点整数。
    返回 'added' / 'updated' / 'skipped'。
    """
    qq = str(qq or "").strip()
    if not qq:
        return "skipped"

    with conn() as c:
        exists = c.execute("SELECT 1 FROM accounts WHERE qq=?", (qq,)).fetchone()
        now = int(time.time())
        if not exists:
            c.execute("""INSERT INTO accounts(qq,nick,gs_id,gs_code,exp_ts,
                                              state,login_at)
                         VALUES(?,?,?,?,0,?,?)""",
                      (qq, str(nick or ""), "", "", "never", now))
            result = "added"
        else:
            result = "updated"

        sets, params = [], []
        if nick:
            sets.append("nick=?"); params.append(str(nick))
        if gs_id:
            sets.append("gs_id=?"); params.append(_enc(str(gs_id)))
        if gs_code:
            gc = str(gs_code)
            sets.append("gs_code=?"); params.append(_enc(gc))
            sets.append("exp_ts=?"); params.append(jwt_exp_safe(gc))
            sets.append("state='ok'"); sets.append("err=''")
        if score is not None:
            sets.append("score=?"); params.append(int(score))
            sets.append("mtime=?"); params.append(int(mtime or now))
        if qb is not None:
            sets.append("qb=?"); params.append(int(round(float(qb) * 100)))
            sets.append("qb_mtime=?"); params.append(now)
        if note is not None:
            sets.append("note=?"); params.append(str(note))
        if query_at:
            sets.append("query_at=?"); params.append(int(query_at))

        if sets:
            params.append(qq)
            c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE qq=?", params)
    return result


def jwt_exp_safe(gs_code):
    """延迟导入 utils，避免 store 与 login/utils 形成循环依赖。"""
    try:
        from utils import jwt_exp
        return int(jwt_exp(gs_code) or 0)
    except Exception:
        return 0

