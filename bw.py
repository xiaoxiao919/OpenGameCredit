"""黑白号检测（mie 活动接口探针）。

链路来源：第三方样本 Q币信用分查询.exe 函数 0x9302 的静态分析 + 真机验证。

  一次 QQ 互联扫码 → openid + access_token
  GET https://go.mie.gamecenter.qq.com/ams3.0.php
        ?_c=Page&mode=true&game=&nickName=&actid=10134500&checkInstall=false
        &autosvr=true&acttk=gvgpur&rightbtn=false&serverList=&sid=&platid=1
        &area=2&plat=0&tt=2&roleid=1&partition=1&opengamecenter=true&cache_hit=4
        &area_selector=1&invite_uin=&g_tk=&pvsrc=&s_p=1%7Chttp%7C&s_v=8.4.5.4745
        &ozid=&vipid=&
      Cookie: appid=<token 所属 appid>; openid=…; access_token=…; _b=Gameapp

  只看返回 JSON 的 msg（这是样本里那三行字面量比较的原样）：
      仓库券礼包领取参数不满足                        → 白   (ret=12699011)
      天啦噜，活动实在太火爆了，待会儿再来瞅瞅呗！      → 失效 (ret=10002)
      其它，典型如 对不起，您的账号存在异常，无法参与活动！ → 黑   (ret=10700)

★ appid 必须与 token 的签发方一致：
  样本用 101477677（它自己的票）；本项目 pay 侧的票是 101502376 签发的，
  所以这里默认 101502376。用错 appid 会一律退化成"活动太火爆"。
★ 同一个 msg 只说明"这个活动接口对当前账号的应答"，不是信用分接口的结论。
"""
import requests

from pay import PAY_CLIENT_ID

MODE_URL = ("https://go.mie.gamecenter.qq.com/ams3.0.php?_c=Page&mode=true&game="
            "&nickName=&actid=10134500&checkInstall=false&autosvr=true&acttk=gvgpur"
            "&rightbtn=false&serverList=&sid=&platid=1&area=2&plat=0&tt=2&roleid=1"
            "&partition=1&opengamecenter=true&cache_hit=4&area_selector=1"
            "&invite_uin=&g_tk=&pvsrc=&s_p=1%7Chttp%7C&s_v=8.4.5.4745&ozid=&vipid=&")

UA = "Mozilla/4.0 (compatible; MSIE 9.0; Windows NT 6.1)"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-cn",
    "Connection": "Keep-Alive",
    "Host": "go.mie.gamecenter.qq.com",
    "User-Agent": UA,
}

MSG_WHITE = "仓库券礼包领取参数不满足"
MSG_BUSY  = "天啦噜，活动实在太火爆了，待会儿再来瞅瞅呗！"

RET_WHITE, RET_BUSY, RET_BLACK = 12699011, 10002, 10700

WHITE, BLACK, DEAD = "白", "黑", "失效"


class BwResult:
    """verdict: 白 / 黑 / 失效 / 未知；ok 仅当判出了白或黑"""

    __slots__ = ("verdict", "ret", "msg", "reason")

    def __init__(self, verdict, ret=0, msg="", reason=""):
        self.verdict, self.ret, self.msg, self.reason = verdict, ret, msg, reason

    @property
    def ok(self):
        return self.verdict in (WHITE, BLACK)

    def __repr__(self):
        return f"<BwResult {self.verdict} ret={self.ret} msg={self.msg!r}>"


def judge(msg, ret=0):
    """样本里的三分类，逐字照抄；ret 只作为 msg 缺失时的兜底。"""
    if msg == MSG_WHITE:
        return WHITE
    if msg == MSG_BUSY:
        return DEAD
    if msg:
        return BLACK                      # 如「账号存在异常，无法参与活动！」
    return {RET_WHITE: WHITE, RET_BUSY: DEAD, RET_BLACK: BLACK}.get(ret, BLACK)


def check(openid, access_token, appid=PAY_CLIENT_ID, timeout=15, log=lambda *a: None):
    """用 openid + access_token 打活动接口，返回 BwResult。"""
    if not openid or not access_token:
        return BwResult(DEAD, reason="缺少 openid/access_token（需先做支付授权扫码）")

    cookie = (f"appid={appid}; openid={openid}; "
              f"access_token={access_token}; _b=Gameapp")
    headers = dict(HEADERS)
    headers["Cookie"] = cookie

    try:
        r = requests.get(MODE_URL, headers=headers, timeout=timeout,
                         allow_redirects=False)
    except requests.RequestException as e:
        return BwResult("未知", reason=f"网络错误 {type(e).__name__}: {e}")

    if r.status_code != 200:
        return BwResult("未知", reason=f"HTTP {r.status_code}")

    try:
        j = r.json()
    except ValueError:
        return BwResult("未知", reason=f"响应非 JSON: {r.text[:120]}")

    ret = j.get("ret", 0)
    msg = j.get("msg") or ""
    v = judge(msg, ret)
    log(f"[黑白] ret={ret} msg={msg!r} → {v}")
    return BwResult(v, ret, msg)


def check_account(acc, log=lambda *a: None):
    """按 store.all_accounts() 的一条记录直接判黑白。"""
    return check(acc.get("pay_openid", ""), acc.get("pay_token", ""), log=log)
