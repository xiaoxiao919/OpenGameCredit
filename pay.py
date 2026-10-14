"""Q币余额查询（unipay 支付侧）。

链路（全部由真实抓包确证）：
  ① graph.qq.com/oauth2.0/authorize  (client_id=101502376, redirect_uri=pay.qq.com)
      → 302 拿 code  【由 login.PTLogin(target='pay') 完成】
  ② api.unipay.qq.com/v1/r/1450402938/get_qq_userinfo?action=authorization_code
      → 换 access_token(openkey) + openid + refresh_token  【exchange_code】
  ③ api.unipay.qq.com/v1/r/1450000186/wechat_query
      → qb_balance(Q点)，Q币 = qb_balance / 100  【query_qb】
"""
import re
import requests

from utils import build_query, js_uri_component

PAY_CLIENT_ID   = "101502376"
PAY_OFFER_ID    = "1450402938"
PAY_QUERY_OFFER = "1450000186"
PAY_REDIRECT_URI = "https://pay.qq.com/ipay/connect_callback_my.html"

GET_USERINFO_URL = "https://api.unipay.qq.com/v1/r/%s/get_qq_userinfo" % PAY_OFFER_ID
WECHAT_QUERY_URL = ("https://api.unipay.qq.com/v1/r/%s/wechat_query" % PAY_QUERY_OFFER)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


class PayError(Exception):
    pass


def exchange_code(code, log=lambda *a: None):
    """用一次性 code 换 access_token/openid/refresh_token。

    ★ 无 Cookie（抓包 Cookie 空，纯无状态，靠 code 一次性兑换）。
    ★ redirect_uri 最终需「双层编码」：build_query 会对值再 quote 一次，
      故这里只做单层 js_uri_component，最终 = %253A（与抓包逐字节一致）。
      （曾误做双层 js_uri_component → build_query 再 quote → 三层 %25253A，
       导致 unipay 解析失败返回 ret=3900 系统繁忙。）"""
    redirect_arg = js_uri_component(PAY_REDIRECT_URI)
    url = GET_USERINFO_URL + "?" + build_query([
        ("offer_id", PAY_OFFER_ID), ("action", "authorization_code"),
        ("redirect_uri", redirect_arg), ("code", code)])

    try:
        r = requests.get(url, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://pay.qq.com",
            "Referer": "https://pay.qq.com/",
        }, timeout=20)
    except requests.RequestException as e:
        raise PayError(f"get_qq_userinfo 网络错误 {type(e).__name__}: {e}")

    log(f"[Q币②] get_qq_userinfo HTTP {r.status_code} body={r.text[:200]}")
    if r.status_code != 200:
        raise PayError(f"get_qq_userinfo HTTP {r.status_code}\n{r.text[:300]}")
    try:
        j = r.json()
    except ValueError:
        raise PayError(f"get_qq_userinfo 非 JSON：{r.text[:300]}")
    if j.get("ret") != 0:
        raise PayError(f"get_qq_userinfo ret={j.get('ret')} msg={j.get('msg')}\n{j}")
    info = j.get("info", {})
    if not info.get("access_token") or not info.get("openid"):
        raise PayError(f"get_qq_userinfo 缺 access_token/openid：{info}")
    return info


def query_qb(openid, openkey, log=lambda *a: None):
    """查余额。返回 dict: {ret, qb_balance(Q点), qd_balance(Q点), q币(换算后)}"""
    url = WECHAT_QUERY_URL + "?" + build_query([
        ("cmd", "4"), ("pf", "vip_m-pay_html5-html5"), ("pfkey", "pfkey"),
        ("from_h5", "1"), ("from_https", "1"), ("format", "jsonp__getQBBalance"),
        ("openid", openid), ("openkey", openkey),
        ("session_id", "openid"), ("session_type", "kp_accesstoken"),
        ("qq_appid", PAY_CLIENT_ID)])

    r = requests.get(url, headers={
        "User-Agent": UA, "Accept": "*/*",
        "Referer": "https://my.pay.qq.com/",
    }, timeout=15)

    log(f"[Q币③] wechat_query HTTP {r.status_code} body={r.text[:200]}")
    m = re.search(r"_getQBBalance\((.*)\)", r.text, re.S)
    if not m:
        raise PayError(f"wechat_query 返回异常：{r.text[:300]}")
    import json
    try:
        j = json.loads(m.group(1))
    except ValueError:
        raise PayError(f"wechat_query JSON 解析失败：{r.text[:300]}")
    qb_point = int(j.get("qb_balance", 0))
    return {"ret": j.get("ret"), "qb_balance": qb_point,
            "qd_balance": int(j.get("qd_balance", 0)),
            "q币": qb_point / 100.0}


def resolve_pay(code, log=lambda *a: None):
    """pay 登录结果收尾：code → 换 token → 查余额。返回 dict：
       {openid, access_token, refresh_token, expires_in, qb_balance, qd_balance, q币}"""
    info = exchange_code(code, log)
    bal = query_qb(info["openid"], info["access_token"], log)
    return {
        "openid": info["openid"],
        "access_token": info["access_token"],
        "refresh_token": info.get("refresh_token", ""),
        "expires_in": int(info.get("expires_in", "5184000")),
        "qb_balance": bal["qb_balance"],
        "qd_balance": bal["qd_balance"],
        "q币": bal["q币"],
    }


def refresh_qb(openid, token, log=lambda *a: None):
    """用已保存的 openid + access_token 直接查余额（无需重新扫码）。
       返回 {qb_balance, qd_balance, q币}，token 失效抛 PayError。"""
    return query_qb(openid, token, log)