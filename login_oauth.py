"""单码登录：一次 QQ 互联扫码，拿到能同时查「信用分 / Q币 / 黑白」的凭据。

协议取自第三方样本 Q币信用分查询.exe 的静态分析，并已真机跑通：

  ① xui.ptlogin2.qq.com/ssl/ptqrshow
       ?s=8&e=0&appid=716027609&type=0&t=<rand>&u1=http://connect.qq.com
       &daid=381&pt_3rd_aid=101477677                     ← 二维码 PNG + qrsig
  ② xui.ptlogin2.qq.com/ssl/ptqrlogin
       ?u1=…&ptqrtoken=hash33(qrsig)&daid=381&aid=716027609
       &pt_3rd_aid=101477677&pt_openlogin_data=<response_type=token…>
       → ptuiCB('0','0',<url>,…)，url 里带 openid / access_token / pay_token
  ③ api.unipay.qq.com/v1/r/1450000186/wechat_query?cmd=1
       openid=…&openkey=<token>&session_id=openid&session_type=kp_accesstoken
       → {"ret":0,"nick":"…","provide_uin":"2781743050"}   ← QQ 号唯一来源

★ 为什么 QQ 号要从 cmd=1 拿：这条 H5 授权链路 ptuiCB 第 7 位是空、
  Cookie 里只有 qrsig、credit/login_status 都不返回 uin，只有 unipay 给 provide_uin。
"""
import time
from dataclasses import dataclass

import requests

from pay import userinfo, PayError
from utils import hash33, js_random, ms_now, parse_ptuicb

PT_BASE = "https://xui.ptlogin2.qq.com"
APPID, DAID, THIRD_AID = "716027609", "381", "101477677"
U1_RAW = "http%3A%2F%2Fconnect.qq.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

TOKEN_TTL = 60 * 86400          # QQ 互联 access_token 有效期 60 天

# pt_openlogin_data 原文（771B，手机 H5 授权上下文），一个字节都没改
OPENLOGIN = (
    "&daid=381&aid=716027609&pt_3rd_aid=101477677&pt_openlogin_data="
    "which%3D%26refer_cgi%3Dm_authorize%26response_type%3Dtoken%26client_id%3D101477677"
    "%26state%3Dtest%26display%3D%26openapi%3D1011_1030%26switch%3D1%26src%3D1"
    "%26sdkv%3D3.5.14_full%26sdkp%3Di%26tid%3D1709376224%26pf%3D%26need_pay%3D0"
    "%26browser%3D0%26browser_error%3D%26serial%3D%26token_key%3D"
    "%26redirect_uri%3Dauth%253A%252F%252Fwww.qq.com%26sign%3D%26time%3D"
    "%26status_version%3D%26status_os%3D16.5%26status_machine%3DiPhone14%252C3"
    "%26page_type%3D1%26has_auth%3D1%26update_auth%3D1%26auth_time%3D1709376224327"
    "%26loginfrom%3D%26h5sig%3DyRAxSMW9LxK_t89xkjofPxMCyjav_LJx7OF_lZn3TKs"
    "%26loginty%3D7%26&device=2&ptopt=1&pt_uistyle=35&jsver=v1.48.1"
    "&aegis_uid=3aa67f00000171f5-3b567d4194bdf423-8269&r=0.19277843288981933"
)

CODE_TEXT = {"66": "等待扫码", "67": "已扫码，请在手机上确认",
             "65": "二维码已过期", "71": "二维码校验失败", "0": "登录成功"}


class LoginError(Exception):
    pass


class QrExpired(LoginError):
    pass


@dataclass
class OAuthResult:
    qq: str
    nick: str
    openid: str
    access_token: str
    pay_token: str = ""
    exp_ts: int = 0

    @property
    def appid(self):
        return THIRD_AID


class Qr:
    """给 UI 用的二维码载体，字段名与旧 PTLogin 的 QrSession 保持一致（png）。"""

    def __init__(self, png, qrsig, ptqrtoken, issued):
        self.png, self.qrsig, self.ptqrtoken, self.issued = png, qrsig, ptqrtoken, issued


class OAuthPTLogin:
    def __init__(self, logger=None):
        self.log = logger or (lambda *a: None)
        self.s = requests.Session()
        self.s.trust_env = False          # 不吃系统代理
        self.s.headers.update({"User-Agent": UA,
                               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                               "Accept-Encoding": "gzip, deflate"})

    # ── ① 取二维码 ──────────────────────────────────────
    def step_qr(self) -> Qr:
        url = (f"{PT_BASE}/ssl/ptqrshow?s=8&e=0&appid={APPID}&type=0&t={js_random()}"
               f"&u1={U1_RAW}&daid={DAID}&pt_3rd_aid={THIRD_AID}")
        r = self.s.get(url, headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": f"{PT_BASE}/"}, allow_redirects=False, timeout=20)
        if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
            raise LoginError(f"ptqrshow 异常 HTTP {r.status_code} {len(r.content)}B")
        qrsig = self.s.cookies.get("qrsig", "")
        if not qrsig:
            raise LoginError("ptqrshow 未下发 qrsig")
        self.log(f"[①] PNG {len(r.content)}B qrsig {len(qrsig)}字符")
        return Qr(r.content, qrsig, hash33(qrsig), time.time())

    # ── ② 轮询 ─────────────────────────────────────────
    def step_poll(self, qr: Qr, on_state=None, interval=2.0, timeout=180,
                  should_stop=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if should_stop and should_stop():
                raise LoginError("用户取消")
            url = (f"{PT_BASE}/ssl/ptqrlogin?u1={U1_RAW}&from_ui=1&type=1&ptlang=2052"
                   f"&ptqrtoken={qr.ptqrtoken}{OPENLOGIN}"
                   f"&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052"
                   f"&action=0-0-{ms_now()}")
            r = self.s.get(url, headers={"Accept": "*/*", "Referer": f"{PT_BASE}/"},
                           allow_redirects=False, timeout=20)
            body = r.content.decode("utf-8", "replace")
            f = parse_ptuicb(body)
            if not f or len(f) < 5:
                raise LoginError(f"ptuiCB 解析失败 HTTP {r.status_code} body={body[:300]!r}")

            code, msg = f[0], f[4]
            if on_state:
                on_state(code, CODE_TEXT.get(code, msg), int(deadline - time.time()))
            self.log(f"[②] code={code} {CODE_TEXT.get(code, msg)}")

            if code == "0":
                return f
            if code == "65":
                raise QrExpired("二维码已过期，请重新获取")
            if code == "71":
                raise LoginError("ptqrtoken 与 qrsig 不匹配")
            if code not in ("66", "67"):
                raise LoginError(f"扫码失败 code={code} msg={msg}")
            time.sleep(interval)
        raise QrExpired(f"轮询超时 {timeout}s")

    # ── ③ 从回调里取票 ──────────────────────────────────
    @staticmethod
    def parse_tokens(url: str) -> dict:
        from urllib.parse import unquote
        out = {}
        for txt in (url, unquote(url)):
            for key in ("access_token", "openid", "pay_token"):
                if key not in out:
                    import re
                    m = re.search(rf"[?&#]{key}=([^&#/\s'\"]+)", txt)
                    if m:
                        out[key] = m.group(1)
        return out

    # ── ④ 换 QQ 号 + 昵称 ────────────────────────────────
    def step_identity(self, openid, token, nick_hint=""):
        try:
            info = userinfo(openid, token)
            self.log(f"[④] provide_uin={info['uin']} nick={info['nick']!r}")
            return info["uin"], info["nick"] or nick_hint
        except PayError as e:
            raise LoginError(
                f"扫码成功但换不到 QQ 号：{e}\n"
                f"（openid={openid[:8]}… token={token[:8]}… 已拿到，"
                f"如果只是 userinfo 临时抽风，可稍后重试）")

    # ── 编排 ───────────────────────────────────────────
    def run(self, qr_ready, on_state=None, should_stop=None, timeout=180) -> OAuthResult:
        qr = self.step_qr()
        qr_ready(qr)
        f = self.step_poll(qr, on_state, timeout=timeout, should_stop=should_stop)
        url = f[2]
        tok = self.parse_tokens(url)
        if not (tok.get("access_token") and tok.get("openid")):
            raise LoginError(f"回调里没拿到 openid/access_token：{url[:200]}")
        nick_hint = f[5] if len(f) > 5 else ""
        uin, nick = self.step_identity(tok["openid"], tok["access_token"], nick_hint)
        return OAuthResult(qq=uin, nick=nick or "",
                           openid=tok["openid"],
                           access_token=tok["access_token"],
                           pay_token=tok.get("pay_token", ""),
                           exp_ts=int(time.time()) + TOKEN_TTL)
