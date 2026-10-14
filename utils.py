"""纯计算工具。不联网、不碰 Cookie。带真实抓包向量的自检。"""
import base64, json, random, re, time
from urllib.parse import quote

# ── encodeURIComponent 等价实现 ────────────────────────────────
# JS 不转义: A-Z a-z 0-9 - _ . ! ~ * ' ( )
# Python quote(safe='') 会转义 ! * ' ( )，必须补进 safe
# ★ 关键: login_sig 含 '*'，抓包证实浏览器发的是裸 '*' 而非 %2A
def js_uri_component(s: str) -> str:
    return quote(s, safe="!*'()")

def build_query(pairs) -> str:
    """手工拼查询串，绝不让 requests 自己 urlencode（它用 quote_plus，*→%2A）"""
    return "&".join(f"{k}={js_uri_component(str(v))}" for k, v in pairs if v is not None)

# ── \xNN 反转义 ────────────────────────────────────────────────
# 必须单遍扫描: pt.ptui.href 里嵌套了已 URL 编码的参数
#   s_url\x3Dhttps\x253A\x252F\x252F...  →  \x25→"%"，重复替换会二次解码毁掉
_X_HEX = re.compile(r"\\x([0-9a-fA-F]{2})")
_X_UNI = re.compile(r"\\u([0-9a-fA-F]{4})")

def js_unescape(s: str) -> str:
    s = _X_HEX.sub(lambda m: chr(int(m.group(1), 16)), s)
    return _X_UNI.sub(lambda m: chr(int(m.group(1), 16)), s)

# ── Hash33: ptqrtoken / g_tk ──────────────────────────────────
# hash=5381; hash += (hash<<5) + c;  return hash & 0x7fffffff
# ★ 每步 & 0xFFFFFFFF 不可省: 5381 × 33^128 远超 64 位
def hash33(s: str) -> int:
    """→ 31 位。用于 ptqrtoken / g_tk / hash_clientkey / pt_guid_token"""
    h = 0
    for ch in s:
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    return h & 0x7FFFFFFF

def gtk_hash33(s: str) -> int:
    """QQ互联/Qzone 版 g_tk —— ★ 与 ptlogin 的 hash33 同名不同物！

    ptlogin 版 (c_login_2.js @14788):  e = 0     → 用于 ptqrtoken / hash_clientkey
    QQ互联 版 (graph.qq.com show页):   e = 5381  → 用于 g_tk / auth_token

    两者结构完全相同（e += (e<<5) + c，等价 33e+c mod 2^31），只有初值差。
    踩坑实录: 用 init=0 算 g_tk 得 617886597，真实值 696241162 → authorize 必然失败。
    """
    h = 5381
    for ch in s:
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    return h & 0x7FFFFFFF


def time33(s: str) -> int:
    """→ 32 位。用于 auth_token=time33(supertoken)（静默续期时用）"""
    h = 0
    for ch in s:
        h = (h * 33 + ord(ch)) % 4294967296
    return h

# ── ptuiCB 变长解析 ───────────────────────────────────────────
# ★ 按单引号切分取奇数下标，而非按 "','" 切分
#   登录成功时是 "'登录成功！', '4'" —— 逗号后有空格，按 ',' 切会带残留空格
# 字段(1-based): [1]code [2]ret [3]url [4]state [5]msg [6]昵称 [7]可选
_PTUICB = re.compile(r"ptuiCB\((.*)\)\s*$", re.S)

def parse_ptuicb(text: str):
    m = _PTUICB.search(text.strip())
    if not m:
        return None
    return [p.strip() for p in m.group(1).split("'")[1::2]]

# ── JWT ───────────────────────────────────────────────────────
def jwt_payload(jwt: str) -> dict:
    parts = jwt.split(".")
    if len(parts) < 2:
        return {}
    p = parts[1]
    p += "=" * (-len(p) % 4)          # base64url 补填充
    try:
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}

def jwt_exp(jwt: str) -> int:
    """凭据到期时间戳。★ 批量调度的核心: 不靠试错，直接算还剩几天"""
    try:
        return int(jwt_payload(jwt).get("exp", 0))
    except (ValueError, TypeError):
        return 0

def days_left(exp_ts: int) -> int:
    if exp_ts <= 0:
        return 0
    return int((exp_ts - time.time()) // 86400)

def ms_now() -> int:
    return int(time.time() * 1000)

def js_random() -> str:
    """ptqrshow 的 t= 参数，等价 JS 的 Math.random()"""
    return str(random.random())          # '0.49033099838775607'

def decode_ptnick(hexval: str) -> str:
    """ptnick_{uin} = 昵称 UTF-8 字节的 hex 编码
       实证: ptnick_<uin>="34" → "4"，与同响应 ptuiCB[6]='4' 一致"""
    if not hexval: return ""
    try:
        return bytes.fromhex(hexval).decode("utf-8", "replace")
    except ValueError:
        return ""