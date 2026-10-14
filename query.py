"""信用分查询。★ 每次调用新建 Session，零串号可能。"""
import requests
from dataclasses import dataclass
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

LOGIN_STATUS_URL = "https://gamecredit.qq.com/api/credit/login_status"
QUERY_URL = "https://gamecredit.qq.com/api/qq/proxy/query_common_credit"
REFERER   = "https://gamecredit.qq.com/static/web/index.html"

@dataclass
class QueryResult:
    ok: bool
    score: int = 0
    mtime: int = 0
    rank: float = 0.0
    stars: int = 0
    reason: str = ""
    expired: bool = False      # True = 凭据失效，需重扫

def query_credit(gs_id: str, gs_code: str, timeout=15) -> QueryResult:
    """GET，零参数，零 body。身份完全由 gs_code 里的 account 字段决定。
    ⚠ 只发 gs_id + gs_code 两个 Cookie，其余 30 个埋点 Cookie 全是噪声。"""
    try:
        r = requests.get(QUERY_URL, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            # ★ 这个头是 axios 残留，GET 无 body 也照发（零成本，保持一致）
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Referer": REFERER,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Cookie": f"gs_id={gs_id}; gs_code={gs_code}",
            # ★ 不发 priority（Chrome 的 HTTP/2 扩展，发不了也不需要）
            # ★ 不发 If-None-Match（响应带 ETag，回传会得 304 空 body）
        }, timeout=timeout, allow_redirects=False)
    except requests.RequestException as e:
        return QueryResult(False, reason=f"网络错误 {type(e).__name__}: {e}")

    if r.status_code in (301, 302, 303, 307, 308):
        return QueryResult(False, reason=f"被重定向({r.status_code})，凭据可能已失效",
                           expired=True)
    if r.status_code == 304:
        return QueryResult(False, reason="304 空响应（误发了缓存头？）")
    if r.status_code != 200:
        return QueryResult(False, reason=f"HTTP {r.status_code}")

    try:
        j = r.json()
    except ValueError:
        return QueryResult(False, reason=f"响应非 JSON: {r.text[:120]}")

    # query.py — 替换 j.get("err") != 0 那个分支
    err = j.get("err")
    if err != 0:
        msg = f"err={err} {j.get('errmsg','')}"
        # ★ 实测: 无凭据/凭据失效 → err=-6 "GsError -6 <auth failed>"
        #   这是唯一确定的失效码，不再靠关键字猜
        expired = (err == -6)
        return QueryResult(False, reason=msg, expired=expired)

    d = j.get("data") or {}
    return QueryResult(True,
                       score=int(d.get("score", 0)),
                       mtime=int(d.get("mtime", 0)),
                       rank=float(d.get("rank", 0.0)),   # ★ float，不是 int
                       stars=int(d.get("stars", 0)))

def query_login_status(gs_id: str, gs_code: str, timeout=15) -> dict:
    """GET /api/credit/login_status
       → {"err":0,"data":{"login_type":1,"nickname":"...","head_url":"..."}}
       用途: ① 权威昵称  ② 比 query_common_credit 更语义化的凭据探针
       失败返回 {}"""
    try:
        r = requests.get(LOGIN_STATUS_URL, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://gamecredit.qq.com/static/web/index.html",
            "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Cookie": f"gs_id={gs_id}; gs_code={gs_code}",
        }, timeout=timeout, allow_redirects=False)
        if r.status_code != 200:
            return {}
        j = r.json()
        if j.get("err") != 0:
            return {"err": j.get("err"), "errmsg": j.get("errmsg", "")}
        return j.get("data") or {}
    except Exception:
        return {}