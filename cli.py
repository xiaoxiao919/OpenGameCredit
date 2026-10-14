"""python -m gamecredit.cli login   → 扫码添加账号
   python -m gamecredit.cli refresh → 批量刷新
   python -m gamecredit.cli list    → 查看账号库
   python -m gamecredit.cli diag QQ → 最小 Cookie 诊断"""
import sys, time, pathlib
from login import PTLogin, LoginError, QrExpired
from query import query_credit
from store import all_accounts, upsert, update_result, conn, delete
from worker import Scheduler
from utils import jwt_exp, days_left

def log(*a): print(*a, flush=True)

def cmd_login(target="credit"):
    qr_path = pathlib.Path("qr.png")
    label = "支付(Q币)" if target == "pay" else "信用分"
    def show(qr):
        qr_path.write_bytes(qr.png)
        log(f"\n[{label}] 二维码 → {qr_path.resolve().absolute()}")
        log("请用手机 QQ 扫码，扫完回车开始轮询…")
        input()
    try:
        r = PTLogin(log, target=target).run(
            qr_ready=show, on_state=lambda c, d, s: log(f"  [{c}] {d} (剩 {s}s)"))
        if target == "pay":
            from store import update_pay
            import pay as paymod
            log("\n扫码成功，正在兑换 Q币凭据…")
            info = paymod.resolve_pay(r.pay_code, log)
            update_pay(r.qq, info["openid"], info["access_token"],
                       info["refresh_token"],
                       int(time.time()) + info["expires_in"], info["qb_balance"],
                       nick=r.nick)
            log(f"\n✅ 支付授权成功 QQ={r.qq} 昵称={r.nick}")
            log(f"   Q币余额 = {info['qb_balance']/100.0:.2f}（{info['qb_balance']} Q点）")
            return
        upsert(r)
        log(f"\n✅ 成功 QQ={r.qq} 昵称={r.nick} 凭据剩余={days_left(r.exp_ts)}天")
        log(f"   跳转链 {len(r.trace)} 跳:")
        for t in r.trace:
            log(f"     [{t['stage']}-{t['hop']}] {t['code']} {t['url'][:100]}")
    except QrExpired as e: log(f"\n⏰ 二维码过期: {e}")
    except LoginError as e: log(f"\n❌ 登录失败:\n{e}")
    except Exception as e: log(f"\n❌ {type(e).__name__}: {e}")

def cmd_refresh(mode="today"):
    accs = all_accounts()
    now = time.time()
    tb = time.localtime(now)
    today0 = int(time.mktime((tb.tm_year, tb.tm_mon, tb.tm_mday, 0, 0, 0, 0, 0, -1)))
    if mode == "today":
        accs = [a for a in accs if a["query_at"] < today0]   # ★ 看本地查询时刻，非分数数据时间
    if not accs:
        log("无需刷新"); return
    log(f"待刷新 {len(accs)} 个账号")
    def on_res(acc, res, err):
        if res and res.ok:
            update_result(acc["qq"], res, "ok")
            log(f"  ✅ {acc['qq']} score={res.score} mtime={res.mtime}")
        else:
            state = "expired" if (res and res.expired) else "error"
            update_result(acc["qq"], res or type("R",(),{"score":0,"rank":0.0,"stars":0,"mtime":0})(), state, err)
            log(f"  ❌ {acc['qq']} {err}")
    s = Scheduler(workers=3)
    s.refresh(accs, on_res, lambda d,t: print(f"\r  进度 {d}/{t}", end="", flush=True))
    print()

def cmd_list():
    print(f"{'QQ号':<14}{'昵称':<14}{'分数':<7}{'Q币':<7}{'凭据剩余':<10}{'状态':<9}{'分数更新'}")
    for a in all_accounts():
        mt = time.strftime("%m-%d %H:%M", time.localtime(a["mtime"])) if a["mtime"] else "-"
        qb = f"{a['qb']/100:.2f}" if a['qb'] else "-"
        print(f"{a['qq']:<14}{(a['nick'] or '-'):<14}{a['score']:<7}{qb:<7}"
              f"{str(days_left(jwt_exp(a['gs_code'])))+'天':<10}{a['state']:<9}{mt}")

def cmd_qb():
    """查 Q币余额。有 token 直接查；无 token 提示先重新登录扫码。"""
    import pay as paymod
    from store import update_pay
    ran = 0
    for a in all_accounts():
        if not (a["pay_openid"] and a["pay_token"]):
            log(f"  ⏭ {a['qq']} 无 pay 凭据，需重新扫码登录获取"); continue
        try:
            bal = paymod.query_qb(a["pay_openid"], a["pay_token"], log)
            qb_num = bal["qb_balance"] / 100.0
            update_pay(a["qq"], a["pay_openid"], a["pay_token"], a["pay_refresh"],
                       a["pay_exp_ts"], bal["qb_balance"])
            log(f"  ✅ {a['qq']} {bal['qb_balance']}Q点 = {qb_num} Q币")
            ran += 1
        except Exception as e:
            log(f"  ❌ {a['qq']} {e}")
    if not ran:
        log("没有可查的账号")

def cmd_diag(qq):
    a = next((x for x in all_accounts() if x["qq"] == qq), None)
    if not a: log(f"账号 {qq} 不存在"); return
    log(f"── 诊断 {qq} ──")
    log(f"gs_id 长度={len(a['gs_id'])}  gs_code 长度={len(a['gs_code'])}")
    p = __import__("gamecredit.utils", fromlist=["jwt_payload"]).jwt_payload(a["gs_code"])
    log(f"JWT account={p.get('account')} appid={p.get('appid')} exp={p.get('exp')} 剩余{days_left(p.get('exp',0))}天")
    r = query_credit(a["gs_id"], a["gs_code"])
    log(f"结果: ok={r.ok} score={r.score} mtime={r.mtime} rank={r.rank} stars={r.stars}")
    if not r.ok: log(f"原因: {r.reason}")

def cmd_rescue():
    from store import load_rescue
    r = load_rescue()
    if r:
        log(f"✅ 已从 rescue 文件恢复 QQ={r.qq} 昵称={r.nick} "
            f"剩余={days_left(r.exp_ts)}天")
    else:
        log("没有 rescue 文件")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "login":
        target = sys.argv[2] if len(sys.argv) > 2 else "credit"
        cmd_login(target)
    else:
        {"list": cmd_list, "rescue": cmd_rescue, "qb": cmd_qb,
         "diag": lambda: cmd_diag(sys.argv[2])}.get(
            cmd, lambda: cmd_refresh(sys.argv[2] if len(sys.argv) > 2 else "today"))()
