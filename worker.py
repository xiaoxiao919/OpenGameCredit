"""批量调度：线程池 + 全局限速 + 退避重试 + 熔断。"""
import threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from query import query_credit
from utils import jwt_exp

class Scheduler:
    def __init__(self, workers=3, min_interval=0.3, breaker=5, query=None):
        self.workers = min(workers, 5)
        self.min_interval = min_interval
        self.breaker = breaker          # 连续 N 个失败 → 熔断
        # ★ 查询函数可注入：单码账号走 token 查分，老账号走 gs_id/gs_code
        self.query = query or (lambda a: query_credit(a["gs_id"], a["gs_code"]))
        self._lock = threading.Lock()
        self._last = 0.0
        self._consec_fail = 0
        self.stop_flag = False

    def _throttle(self):
        """全局令牌间隔，不是每线程限流"""
        with self._lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0: time.sleep(wait)
            self._last = time.time()

    def _one(self, acc):
        if self.stop_flag: return acc, None, "已取消"
        # ★ 发请求前先查 exp，过期的一个包都不发
        exp = jwt_exp(acc.get("gs_code", "")) or int(acc.get("oa_exp", 0) or 0)
        if exp and exp <= time.time():
            return acc, None, "凭据已过期"

        for attempt in range(3):
            if self.stop_flag: return acc, None, "已取消"
            self._throttle()
            res = self.query(acc)
            if res.ok:
                with self._lock: self._consec_fail = 0
                return acc, res, ""
            if res.expired:                 # err == -6，不重试，直接标失效
                return acc, res, res.reason + "（需重新扫码）"      # 失效不重试
            time.sleep([1, 3][min(attempt, 1)])    # 指数退避
        with self._lock: self._consec_fail += 1
        return acc, res, res.reason

    def refresh(self, accounts, on_result, on_progress=None):
        """on_result(acc, res, err) / on_progress(done, total)"""
        self.stop_flag, self._consec_fail = False, 0
        total, done = len(accounts), 0
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(self._one, a): a for a in accounts}
            for f in as_completed(futs):
                acc, res, err = f.result()
                done += 1
                on_result(acc, res, err)
                if on_progress: on_progress(done, total)
                with self._lock:
                    if self._consec_fail >= self.breaker:
                        self.stop_flag = True      # 🔴 熔断：防 IP 被打死
                        for g in futs: g.cancel()