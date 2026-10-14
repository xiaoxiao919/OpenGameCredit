"""QQ 扫码登录 → gs_id/gs_code 换取。

链路（全部由真实抓包确证）：
  ① xlogin   ② ptqrshow   ③ ptqrlogin   ④ check_sig 302
  ⑤ login_jump = postMessage 桥接页（iframe 阶段结束）
  ⑥ GET  graph.qq.com/oauth2.0/show        ← 父窗口，含 authorize 隐藏表单
  ⑦ POST graph.qq.com/oauth2.0/authorize   ← agree() 提交，302 带 code
  ⑧ GET  redirect_uri?code=...             ← 目标：拿到 gs_id/gs_code
"""
import re, time, uuid
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
import requests

from utils import (hash33, gtk_hash33, js_unescape, js_random, build_query,
                   parse_ptuicb, jwt_exp, days_left, ms_now, decode_ptnick)

CONNECT_URL = "https://gamecredit.qq.com/connect"
PT_HOST  = "xui.ptlogin2.qq.com"
PT_BASE  = f"https://{PT_HOST}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
HTML_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8")

DEF_APPID, DEF_DAID, DEF_3RD = "716027609", "383", "102053620"
DEF_S_URL = "https://graph.qq.com/oauth2.0/login_jump"

# ── QQ 互联第三方应用配置，全部来自抓包 ────────────────────────────
# 信用分（gamecredit）与支付（pay.qq.com）是两个不同的 pt_3rd_aid，
# 各自需要独立的扫码登录态（pt_3rd_aid 不同 → 不同 p_skey / 授权会话）。
TARGET_CREDIT = "credit"
TARGET_PAY    = "pay"

PAY_3RD_AID    = "101502376"
PAY_REDIRECT   = "https://pay.qq.com/ipay/connect_callback_my.html"

_TARGETS = {
    TARGET_CREDIT: {
        "pt_3rd_aid": "102053620",
        "s_url":       "https://graph.qq.com/oauth2.0/login_jump",
        "client_id":   "102053620",
        "oauth_state": "qqconnect_1",
        "scope":       "all",
        "redirect_uri": ("https://gamecredit.qq.com/login-ui/index.html"
                         "?cPageName=middle&type=QQ&backUrl=reload&appId=102053620"),
    },
    TARGET_PAY: {
        "pt_3rd_aid": PAY_3RD_AID,
        "s_url":       "https://graph.qq.com/oauth2.0/login_jump",
        "client_id":   PAY_3RD_AID,
        "oauth_state": "",
        "scope":       "",
        "redirect_uri": PAY_REDIRECT,
    },
}

# ── QQ 互联第三方应用（gamecredit）固定配置，全部来自抓包 ──────────
CLIENT_ID    = "102053620"                     # == pt_3rd_aid
OAUTH_STATE  = "qqconnect_1"
REDIRECT_URI = ("https://gamecredit.qq.com/login-ui/index.html"
                "?cPageName=middle&type=QQ&backUrl=reload&appId=102053620")
SHOW_URL     = ("https://graph.qq.com/oauth2.0/show?" + build_query([
                    ("which", "Login"), ("display", "pc"), ("state", OAUTH_STATE),
                    ("client_id", CLIENT_ID), ("response_type", "code"),
                    ("scope", "all"), ("redirect_uri", REDIRECT_URI)]))
AUTHORIZE_URL = "https://graph.qq.com/oauth2.0/authorize"

# ★ 回传会让服务端返 304 空 body（xlogin 的 If-Modified-Since、查询的 ETag）
_DANGEROUS = {"if-none-match", "if-modified-since", "if-unmodified-since", "if-match"}

_PTUI_RE  = re.compile(r"pt\.ptui\s*=\s*\{(.*?)\}\s*;", re.S)
_FIELD_RE = re.compile(r'([A-Za-z_]\w*)\s*:\s*(?:encodeURIComponent\()?\s*"([^"]*)"')
_CDN_RE   = re.compile(r"cdn-go\.cn/monorepo/([0-9a-fA-F]{6,16})")
_BRIDGE_RX = re.compile(r"qclogin_success")
_FORM_RX   = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S | re.I)
_IN_RX1    = re.compile(r"""<input[^>]*?\bname=["']([^"']+)["'][^>]*?\bvalue=["']([^"']*)["']""", re.I)
_IN_RX2    = re.compile(r"""<input[^>]*?\bvalue=["']([^"']*)["'][^>]*?\bname=["']([^"']+)["']""", re.I)
_ATTR_RX   = lambda a: re.compile(rf"""\b{a}\s*=\s*["']([^"']*)["']""", re.I)
_SRC_RX  = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+?\.js[^"']*)["']""", re.I)
_PATH_RX = re.compile(r"""["'`](/(?:api|auth|login|proxy|oauth|qq)[A-Za-z0-9_\-./{}]{1,90})["'`]""")


_JS_REDIRECT = [
    re.compile(r"""location\.href\s*=\s*["']([^"']+)["']"""),
    re.compile(r"""location\.replace\s*\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""cleanCache\s*\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""top\.location[^=]*=\s*["']([^"']+)["']"""),
    re.compile(r"""window\.location\s*=\s*["']([^"']+)["']"""),
]


class LoginError(Exception): pass
class QrExpired(LoginError): pass


def extract_js_redirect(html: str) -> str:
    for rx in _JS_REDIRECT:
        m = rx.search(html)
        if m:
            return js_unescape(m.group(1))
    return ""


def is_bridge_page(body: str) -> bool:
    """login_jump 的 postMessage 桥接页 = iframe 阶段的成功标志，不是失败"""
    return bool(_BRIDGE_RX.search(body)) and "postMessage" in body


def extract_authorize_form(html: str):
    """从 show 页抽 authorize 隐藏表单；抽不到就返回 None 走已知字段集"""
    for m in _FORM_RX.finditer(html):
        attrs, inner = m.group(1), m.group(2)
        am = _ATTR_RX("action").search(attrs)
        action = am.group(1) if am else ""
        if "authorize" not in action.lower():
            continue
        mm = _ATTR_RX("method").search(attrs)
        fields = {k: v for k, v in _IN_RX1.findall(inner)}
        fields.update({k: v for v, k in _IN_RX2.findall(inner)})   # value 在前的写法
        return {"action": action,
                "method": (mm.group(1).upper() if mm else "POST"),
                "fields": fields}
    return None


def safe_headers(h, log=None):
    out = {}
    for k, v in (h or {}).items():
        if k.lower() in _DANGEROUS:
            if log: log(f"       [拦截危险头] {k}")
            continue
        out[k] = v
    return out


def new_session(logger=None):
    """★ 每次登录新建 Session，cookiejar 独立 → 物理杜绝多账号串号"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA,
                      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                      "Accept-Encoding": "gzip, deflate"})   # 去 br/zstd 免装 brotli
    return s


def cookie_names(s) -> set:  return {c.name for c in s.cookies}
def cookie_pairs(s) -> list: return sorted((c.name, c.domain) for c in s.cookies)

def find_cookie(s, name: str) -> str:
    """跨域找（gs_id/gs_code 的 Domain 未知，不能指定 domain 查）"""
    for c in s.cookies:
        if c.name == name and c.value:
            return c.value
    return ""


@dataclass
class QrSession:
    """★ qrsig 与 ptqrtoken 必须绑定同一次 ptqrshow，封进一个对象禁止分开存取"""
    png: bytes; qrsig: str; ptqrtoken: int; issued: float


@dataclass
class LoginResult:
    qq: str; nick: str; gs_id: str; gs_code: str; exp_ts: int
    trace: list = field(default_factory=list)
    super_triple: tuple = ("", "", "")     # 为免扫码续期留档
    pay: dict = None                        # Q币查询结果 {openid,access_token,qb,...}
    pay_code: str = ""                      # pay 目标：authorize 换到的 code
    pay_redirect: str = ""                  # pay 目标：authorize 302 的 Location


class PTLogin:
    CODE_TEXT = {"66": "等待扫码", "67": "已扫码，请在手机上确认",
                 "65": "二维码已过期", "71": "二维码校验失败", "0": "登录成功"}

    def __init__(self, logger=None, target=TARGET_CREDIT):
        if target not in _TARGETS:
            raise LoginError(f"未知登录目标 {target!r}（可选 credit/pay）")
        self.target = target
        self.cfg = _TARGETS[target]
        self.log = logger or (lambda *a: None)
        self.s = new_session()
        self.ptui, self.check_sig_url = {}, ""
        self.qq = self.nick = self.ui_val = ""
        self.auth_form = None
        self.show_actual = self._build_show_url()
        self.trace, self.super_triple = [], ("", "", "")

    def _build_show_url(self):
        """show 页 URL —— 用目标自己的 client_id/redirect_uri/state/scope"""
        q = [("which", "Login"), ("display", "pc"),
             ("client_id", self.cfg["client_id"]), ("response_type", "code"),
             ("redirect_uri", self.cfg["redirect_uri"])]
        st = self.cfg.get("oauth_state")
        if st:
            q.append(("state", st))
        sc = self.cfg.get("scope")
        if sc:
            q.append(("scope", sc))
        return "https://graph.qq.com/oauth2.0/show?" + build_query(q)

    # ══ ① xlogin ══════════════════════════════════════════
    def step1_xlogin(self):
        aid3 = self.cfg["pt_3rd_aid"]
        url = f"{PT_BASE}/cgi-bin/xlogin?" + build_query([
            ("appid", DEF_APPID), ("daid", DEF_DAID), ("style", "33"),
            ("login_text", "登录"), ("hide_title_bar", "1"), ("hide_border", "1"),
            ("target", "self"), ("s_url", self.cfg["s_url"]), ("pt_3rd_aid", aid3),
            ("pt_feedback_link",
             f"https://support.qq.com/products/77942?customInfo=.appid{aid3}"),
            ("theme", "2"), ("verify_theme", "")])      # 空值也要发，抓包证实
        r = self.s.get(url, headers=safe_headers({
            "Accept": HTML_ACCEPT, "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1", "Sec-Fetch-Dest": "iframe",
            "Referer": "https://graph.qq.com/",
        }, self.log), allow_redirects=False, timeout=20)   # ★ 禁自动重定向

        self._parse_ptui(r.content.decode("utf-8", "replace"))
        # ★ login_sig 只有一个数据源：Cookie 里的 pt_login_sig
        #   （HTML 的 pt.ptui.login_sig 是空串，运行时由 JS 回填）
        if not find_cookie(self.s, "pt_login_sig"):
            raise LoginError(f"xlogin 未下发 pt_login_sig (HTTP {r.status_code})")
        self.log(f"[①] pt_login_sig ✅  会话Cookie={sorted(cookie_names(self.s))}")

    def _parse_ptui(self, html):
        m = _PTUI_RE.search(html)
        if not m:
            raise LoginError("页面找不到 pt.ptui，HTML 结构已变更")
        self.ptui = {k: js_unescape(v) for k, v in _FIELD_RE.findall(m.group(1))}
        cm = _CDN_RE.search(html)
        self.ptui["_pt_js_version"] = cm.group(1) if cm else ""
        miss = [k for k in ("appid", "daid", "pt_3rd_aid", "ptui_version",
                            "style", "s_url", "href") if not self.ptui.get(k)]
        if miss:
            raise LoginError(f"pt.ptui 缺字段 {miss}（正则失效）")
        self.log(f"[①] aid={self.ptui['appid']} daid={self.ptui['daid']} "
                 f"3rd={self.ptui['pt_3rd_aid']} js_ver={self.ptui['ptui_version']} "
                 f"uistyle={self.ptui['style']} js={self.ptui['_pt_js_version']}")
        # ★ pt_uistyle 来自 pt.ptui.style(=40)，不是 URL 的 style(=33)；源码 i.pt_uistyle=O.ptui.style 已证实

    # ══ ② ptqrshow ════════════════════════════════════════
    def step2_get_qr(self) -> QrSession:
        url = f"{PT_BASE}/ssl/ptqrshow?" + build_query([
            ("appid", self.ptui["appid"]), ("e", "2"), ("l", "M"), ("s", "3"),
            ("d", "72"), ("v", "4"), ("t", js_random()),
            ("daid", self.ptui["daid"]), ("pt_3rd_aid", self.ptui["pt_3rd_aid"]),
            ("u1", self.ptui["s_url"])])
        r = self.s.get(url, headers=safe_headers({
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "image", "Referer": self.ptui["href"],
        }, self.log), allow_redirects=False, timeout=20)
        if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
            raise LoginError(f"ptqrshow 异常 HTTP {r.status_code} {len(r.content)}B")
        qrsig = find_cookie(self.s, "qrsig")
        if not qrsig:
            raise LoginError("ptqrshow 未下发 qrsig")
        self.log(f"[②] PNG {len(r.content)}B  qrsig {len(qrsig)}字符")
        return QrSession(r.content, qrsig, hash33(qrsig), time.time())

    # ══ ③ ptqrlogin 轮询 ══════════════════════════════════
    def step3_poll(self, qr: QrSession, on_state=None, interval=2.0, timeout=180, should_stop=None):
        deadline, first = time.time() + timeout, True
        while time.time() < deadline:
            if should_stop and should_stop():
                raise LoginError("用户取消")
            url = f"{PT_BASE}/ssl/ptqrlogin?" + build_query([
                ("u1", self.ptui["s_url"]), ("ptqrtoken", str(qr.ptqrtoken)),
                ("ptredirect", "0"), ("h", "1"), ("t", "1"), ("g", "1"),
                ("from_ui", "1"), ("ptlang", self.ptui.get("lang", "2052")),
                ("action", f"0-0-{ms_now()}"),     # ★ 中段固定0；源码 I.action.join("-")+"-"+ +new Date
                ("js_ver", self.ptui["ptui_version"]), ("js_type", "1"),
                ("login_sig", find_cookie(self.s, "pt_login_sig")),
                ("pt_uistyle", self.ptui["style"]),
                ("aid", self.ptui["appid"]), ("daid", self.ptui["daid"]),
                ("pt_3rd_aid", self.ptui["pt_3rd_aid"]),
                ("pt_js_version", self.ptui["_pt_js_version"])])
                # ★ has_onekey / o1vId 已删（抓包对比证实可选）
                # ★ pt_jstoken / pt_ev_token 不发（源码是 && 短路守卫，缺失即不发）
            r = self.s.get(url, headers=safe_headers({
                "Accept": "*/*", "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script",
                "Referer": self.ptui["href"],
            }, self.log), allow_redirects=False, timeout=20)

            body = r.content.decode("utf-8", "replace")
            if first:
                self.log(f"[③] 首轮 HTTP {r.status_code} {len(r.content)}B "
                         f"ptqrtoken={qr.ptqrtoken}")
                first = False
            f = parse_ptuicb(body)
            if not f or len(f) < 5:
                raise LoginError(
                    f"ptuiCB 解析失败\n  HTTP {r.status_code} "
                    f"CL={r.headers.get('Content-Length')} 实际{len(r.content)}B "
                    f"CE={r.headers.get('Content-Encoding')}\n"
                    f"  ptqrtoken={qr.ptqrtoken} qrsig[:16]={qr.qrsig[:16]}\n"
                    f"  body={body[:300]!r}")

            code, msg = f[0], f[4]
            self.nick = f[5] if len(f) > 5 else ""     # ★ [6]=昵称
            if on_state:
                on_state(code, self.CODE_TEXT.get(code, msg), int(deadline - time.time()))
            self.log(f"[③] code={code} {self.CODE_TEXT.get(code, msg)}")

            if code == "0":
                self.check_sig_url = f[2]
                if not self.check_sig_url.startswith("http"):
                    raise LoginError(f"code=0 但 url 异常: {self.check_sig_url[:200]}")
                m = re.search(r"[?&]uin=(\d+)", self.check_sig_url)
                self.qq = m.group(1) if m else ""      # 🎁 QQ号从 check_sig 白送
                self.log(f"[③] ✅ uin={self.qq} nick={self.nick!r}")
                return
            if code in ("66", "67"):
                time.sleep(interval); continue
            if code == "65": raise QrExpired(msg)
            if code == "71": raise LoginError("ptqrtoken 与 qrsig 不匹配")
            raise LoginError(f"未知 code={code} msg={msg} 全字段={f}")
        raise QrExpired(f"轮询超时 {timeout}s")

    # ══ ④⑤ check_sig → login_jump 桥接页 ══════════════════
    def step4_check_sig(self):
        self._follow(self.check_sig_url, "④", max_hops=10,
                     referer=f"{PT_BASE}/", stop_on_bridge=True)

    # ══ ⑥ show 页：拿 ui cookie + 抽表单 ══════════════════
    def step5_show_page(self):
        self.show_actual = self._build_show_url()
        try:
            r = self.s.get(self.show_actual, headers=safe_headers({
                "Accept": HTML_ACCEPT, "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "max-age=0",
                "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1", "Sec-Fetch-Dest": "document",
            }, self.log), allow_redirects=False, timeout=20)
            html = r.content.decode("utf-8", "replace")
            self.log(f"[⑥] show 页 HTTP {r.status_code} {len(html)}B")
            form = extract_authorize_form(html)
            if form:
                self.auth_form = form
                self.log(f"[⑥] 抽到表单 {form['method']} {form['action']} "
                         f"字段={list(form['fields'])}")
            else:
                self.log("[⑥] 未抽到 authorize 表单 → 用抓包已知字段集")
        except Exception as e:
            self.log(f"[⑥] show 页抓取失败({type(e).__name__}) → 用已知字段集")

        # ui: Domain=graph.qq.com 的客户端 UUIDv4（抓包证实）
        self.ui_val = find_cookie(self.s, "ui")
        if self.ui_val:
            self.log(f"[⑥] ui cookie 已存在 {self.ui_val}")
        else:
            self.ui_val = str(uuid.uuid4()).upper()
            self.s.cookies.set("ui", self.ui_val, domain="graph.qq.com", path="/")
            self.log(f"[⑥] show 页未下发 ui → 自行生成 UUID4 {self.ui_val}")

    # ══ ⑦⑧ POST authorize → redirect_uri → gs_id/gs_code ══
    def step6_authorize(self) -> LoginResult:
        p_skey = find_cookie(self.s, "p_skey")
        if not p_skey:
            raise LoginError(f"缺 p_skey@graph.qq.com，阶段一未完成。jar={cookie_pairs(self.s)}")
        g_tk = gtk_hash33(p_skey)                          # ★ 源码/抓包双证
        self.log(f"[⑦] g_tk=gtk_hash33(p_skey)={g_tk}  auth_time={ms_now()}")

        pairs = [("response_type", "code"), ("client_id", self.cfg["client_id"]),
                 ("redirect_uri", self.cfg["redirect_uri"]),
                 ("scope", self.cfg.get("scope", "")),
                 ("state", self.cfg.get("oauth_state", "")),
                 ("switch", ""),
                 ("from_ptlogin", "1"), ("src", "1"), ("update_auth", "1"),
                 ("openapi", "1010")]
        # show 页抽到的静态字段优先（抗腾讯改版），动态三件套始终自算
        if self.auth_form:
            known = dict(pairs)
            for k, v in self.auth_form["fields"].items():
                if k not in ("g_tk", "auth_time", "ui") and k not in known:
                    pairs.append((k, v))
                    self.log(f"[⑦] 表单补充字段 {k}={v[:40]}")
        pairs += [("g_tk", str(g_tk)), ("auth_time", str(ms_now())), ("ui", self.ui_val)]

        body = build_query(pairs)
        self.log(f"[⑦] POST {AUTHORIZE_URL}  body {len(body)}B")
        r = self.s.post(AUTHORIZE_URL, data=body.encode("utf-8"),
                        headers=safe_headers({
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Origin": "https://graph.qq.com",
                            "Referer": self.show_actual,
                            "Accept": HTML_ACCEPT, "Cache-Control": "max-age=0",
                            "Upgrade-Insecure-Requests": "1",
                            "Sec-Fetch-Site": "same-origin",
                            "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe",
                        }, self.log), allow_redirects=False, timeout=20)

        self.log(f"[⑦] HTTP {r.status_code} Location={r.headers.get('Location','')[:150]}")
        txt = r.content.decode("utf-8", "replace")
        if r.status_code not in (301, 302, 303, 307, 308):
            raise LoginError(f"authorize 未返回 302\nHTTP {r.status_code}\n{txt[:600]}")
        loc = r.headers.get("Location")
        if "code=" not in loc:
            raise LoginError(f"authorize 302 但未带 code（可能未授权/需同意页）\n"
                             f"Location={loc}\n{txt[:400]}")
        import re as _re
        m = _re.search(r"[?&]code=([^&]+)", loc)
        if not m:
            raise LoginError(f"Location 里取不到 code\n{loc}")
        self.log(f"[⑦] ✅ code={m.group(1)}（一次性，约5分钟有效），立即兑换")
        code = m.group(1)
        if self.target == TARGET_PAY:
            # pay：code 用于换 unipay token，不在本类里兑换，直接带回
            return self._finish_pay(loc, code)
        return self.step8_exchange(loc, code)

    def step8_exchange(self, redirect_url, code) -> LoginResult:
        """middle.js 的 initLoginToken():
             IBOX.waxios({url: `/connect?code=${e}&appId=${i}&atype=${t.toUpperCase()}`})
           waxios 默认 method=get, baseURL=""(见 ibox-config.json) → 同域根路径。

           ★ 实测该端点无鉴权: code 是唯一凭据（请求里既无 p_skey 也无 gs_code）
           ★ 响应 Set-Cookie 无 Domain 属性 → host-only，仅属 gamecredit.qq.com
           ★ 成功判据是 body 的 ret==0，不是 err
        """
        # ⚠ 源码用模板字符串直接插值（未 encodeURIComponent）。
        #   code 是大写hex、appId 是纯数字，两者编码前后一致，故照抄原样拼接。
        url = f"{CONNECT_URL}?code={code}&appId={self.cfg['client_id']}&atype=QQ"
        self.log(f"[⑧] GET {url[:110]}")

        r = self.s.get(url, headers=safe_headers({
            "Accept": "application/json, text/plain, */*",
            # axios 残留头：GET 无 body 也照发，与浏览器一致
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": redirect_url,
            "Origin": "https://gamecredit.qq.com",
            "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }, self.log), allow_redirects=False, timeout=25)

        body = r.content.decode("utf-8", "replace")
        self.log(f"[⑧] HTTP {r.status_code} body={body[:200]}")
        self.log(f"[⑧] Set-Cookie: {r.headers.get('Set-Cookie', '(无)')[:160]}")

        if r.status_code != 200:
            raise LoginError(f"/connect HTTP {r.status_code}\n{body[:400]}")

        gid = find_cookie(self.s, "gs_id")
        gc = find_cookie(self.s, "gs_code")
        if not (gid and gc):
            raise LoginError(
                f"/connect 返回 200 但未下发 gs_id/gs_code\n"
                f"  body: {body[:400]}\n"
                f"  Set-Cookie: {r.headers.get('Set-Cookie', '(无)')}\n"
                f"  jar: {cookie_pairs(self.s)}")

        try:
            import json as _json
            ret = _json.loads(body).get("ret")
            if ret != 0:
                self.log(f"[⑧] ⚠️ ret={ret}（非0），但仍拿到了 Cookie，继续")
        except ValueError:
            pass

        self.log(f"[⑧] ✅ gs_id={gid[:12]}… gs_code={len(gc)}B")
        return self._finish(gid, gc)

    # ══ 通用 GET 跟随器 ══════════════════════════════════
    def _follow(self, url, tag, max_hops, referer=None, stop_on_bridge=False):
        """★ 唯一成功判据 = jar 里同时出现 gs_id 和 gs_code，中间路径全不管"""
        prev = referer
        for hop in range(1, max_hops + 1):
            hdrs = {"Accept": HTML_ACCEPT, "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Mode": "navigate", "Sec-Fetch-User": "1",
                    "Sec-Fetch-Dest": "iframe",
                    "Sec-Fetch-Site": "same-site" if prev else "none"}
            if prev:
                hdrs["Referer"] = prev
            r = self.s.get(url, headers=safe_headers(hdrs, self.log),
                           allow_redirects=False, timeout=20)
            self.trace.append({"stage": tag, "hop": hop, "code": r.status_code,
                               "url": url, "location": r.headers.get("Location", ""),
                               "sent": sorted(cookie_names(self.s))})
            self.log(f"[{tag}-{hop}] {r.status_code} {url[:120]}")
            self.log(f"          jar={sorted(cookie_names(self.s))}")

            gid, gc = find_cookie(self.s, "gs_id"), find_cookie(self.s, "gs_code")
            if gid and gc:
                self.log(f"[{tag}-{hop}] 🎯 拿到 gs_id/gs_code")
                return self._finish(gid, gc)

            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                if not loc:
                    raise LoginError(f"{tag}-{hop} {r.status_code} 无 Location\n{self.trace}")
                prev, url = url, urljoin(url, loc)
                continue

            body = r.content.decode("utf-8", "replace")

            if stop_on_bridge and is_bridge_page(body):
                self.log(f"[{tag}-{hop}] ✅ 抵达 postMessage 桥接页，iframe 阶段结束")
                return None

            js = extract_js_redirect(body)
            if js:
                self.log(f"          JS 跳转 → {js[:120]}")
                prev, url = url, urljoin(url, js)
                continue

            # ── 走到这里说明是 SPA 壳页，需要 JS 发 XHR 才能继续 ──
            dump = ""
            try:
                from pathlib import Path
                Path(f"debug_{tag}_{hop}.html").write_text(body, encoding="utf-8")
                dump = f"完整响应已落盘 debug_{tag}_{hop}.html"
            except Exception as e:
                dump = f"落盘失败 {e}"
            raise LoginError(
                f"{tag}-{hop} 终止 HTTP {r.status_code}，未拿到 gs_id/gs_code\n"
                f"  URL: {url}\n"
                f"  Set-Cookie: {r.headers.get('Set-Cookie','(无)')[:300]}\n"
                f"  script 资源: {sorted(set(_SRC_RX.findall(body)))[:10]}\n"
                f"  页面内路径: {sorted(set(_PATH_RX.findall(body)))[:15]}\n"
                f"  {dump}\n"
                f"  跳转链: {self.trace}")
        raise LoginError(f"{tag} 超 {max_hops} 跳仍未拿到 gs_code\n{self.trace}")



    def _finish(self, gid, gc):
        self.super_triple = (find_cookie(self.s, "superuin"),
                             find_cookie(self.s, "supertoken"),
                             find_cookie(self.s, "superkey"))
        pn = find_cookie(self.s, f"ptnick_{self.qq}")
        if pn:
            dec = decode_ptnick(pn)
            self.log(f"[✅] ptnick={pn!r} → {dec!r}（ptuiCB 给的是 {self.nick!r}）")
            if dec: self.nick = dec
        if any(self.super_triple):
            self.log(f"[✅] super 三件套已捕获 → 为免扫码续期留档")
        if find_cookie(self.s, "pt_susp_sig"):
            self.log("[⚠️] 检测到 pt_susp_sig，可能触发风控二次验证")
        exp = jwt_exp(gc)
        self.log(f"[✅] exp={exp} 剩余 {days_left(exp)} 天")
        return LoginResult(self.qq, self.nick, gid, gc, exp,
                           self.trace, self.super_triple)

    def _finish_pay(self, redirect_url, code):
        """pay 目标：authorize 拿到的 code 用于换 unipay token，这里只带回 code。
           换 token + 查余额由 pay.py 完成（无 Cookie，靠 code 一次性兑换）。"""
        self.super_triple = (find_cookie(self.s, "superuin"),
                             find_cookie(self.s, "supertoken"),
                             find_cookie(self.s, "superkey"))
        # ★ 与信用分链路一致：用 ptnick_<uin> 的 hex 解码校正昵称
        pn = find_cookie(self.s, f"ptnick_{self.qq}")
        if pn:
            dec = decode_ptnick(pn)
            self.log(f"[✅] ptnick={pn!r} → {dec!r}（ptuiCB 给的是 {self.nick!r}）")
            if dec:
                self.nick = dec
        exp = int(time.time()) + 5184000      # access_token 有效期 60 天
        res = LoginResult(self.qq, self.nick, "", "", exp,
                          self.trace, self.super_triple)
        res.pay_code = code                   # ★ 动态加字段，供调用方换 token
        res.pay_redirect = redirect_url
        return res

    # ══ 编排 ═════════════════════════════════════════════
    def run(self, qr_ready, on_state=None, should_stop=None) -> LoginResult:
        self.step1_xlogin()
        qr = self.step2_get_qr()
        qr_ready(qr)
        self.step3_poll(qr, on_state, should_stop = should_stop)
        self.step4_check_sig()
        self.step5_show_page()
        res = self.step6_authorize()    # → credit: gs_id/gs_code; pay: pay_code

        if self.target == TARGET_PAY:
            # pay 目标到此为止：code 已拿到，换 token / 查余额由调用方衔接
            return res

        # ⑨ 用 login_status 取权威昵称（ptuiCB[6] / ptnick 都只是兜底）
        try:
            from query import query_login_status
            st = query_login_status(res.gs_id, res.gs_code)
            if st.get("nickname"):
                self.log(f"[⑨] login_status: 昵称={st['nickname']!r} "
                         f"login_type={st.get('login_type')}")
                res.nick = st["nickname"]
        except Exception as e:
            self.log(f"[⑨] login_status 失败({type(e).__name__})，沿用昵称 {res.nick!r}")
        return res
