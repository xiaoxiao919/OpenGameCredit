"""app.py — tkinter GUI
   启动: python app.py
   依赖: pip install requests cryptography pillow
"""
import csv, queue, threading, time, tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont
from io import BytesIO
from PIL import Image, ImageTk

from login import PTLogin, LoginError, QrExpired
from query import query_credit
from store import all_accounts, upsert, update_result, delete, APP_DIR
from worker import Scheduler
from utils import jwt_exp, days_left

QR_PX = 140            # 二维码显示边长（NEAREST 放大保持锐利）
POLL_MS = 60           # UI 事件队列轮询间隔

ELLIPSIS = "..."

STATE_TXT = {"ok": "正常", "expired": "凭据过期", "error": "查询失败", "never": "未查询"}
STATE_FG  = {"ok": "#0a7d32", "expired": "#c62828", "error": "#e07800", "never": "#888"}


# ════════════════ 扫码登录面板（内嵌，非弹窗） ════════════════
class ScanPanel(tk.Frame):
    """嵌在右侧栏顶部的扫码区：二维码 + 信用分/支付 圆点单选。"""

    def __init__(self, master, on_credit_ok, on_pay_ok):
        super().__init__(master)
        self.on_credit_ok = on_credit_ok     # 信用分登录成功回调(res)
        self.on_pay_ok = on_pay_ok           # 支付登录成功回调(res)
        self.target = "credit"
        self.q = queue.Queue()
        self._cancel = False
        self._img = None
        self._th = None
        self._gen = 0                        # ★ 代际标记：每次重启 +1，旧线程事件作废

        self.canvas = tk.Canvas(self, width=QR_PX, height=QR_PX,
                                bg="white", highlightthickness=1,
                                highlightbackground="#ccc")
        self.canvas.pack(pady=(0, 4))
        self.canvas.create_text(QR_PX // 2, QR_PX // 2, text="点击下方「获取二维码」\n开始扫码", 
                                fill="#999", font=("Microsoft YaHei UI", 11),
                                justify="center", tags="ph")

        # ── 圆点单选：信用分 / 支付（字前方有 ●） ──
        self.var_target = tk.StringVar(value="credit")
        opt = ttk.Frame(self); opt.pack(pady=(2, 0))
        self.rb_credit = tk.Radiobutton(
            opt, text="信用分", variable=self.var_target, value="credit",
            command=lambda: self.switch_target("credit"),
            font=("Microsoft YaHei UI", 10), indicatoron=True)
        self.rb_pay = tk.Radiobutton(
            opt, text="支付", variable=self.var_target, value="pay",
            command=lambda: self.switch_target("pay"),
            font=("Microsoft YaHei UI", 10), indicatoron=True)
        self.rb_credit.pack(side="left", padx=8)
        self.rb_pay.pack(side="left", padx=8)

        self.var_state = tk.StringVar(value="就绪 · 未获取二维码")
        ttk.Label(self, textvariable=self.var_state, anchor="center",
                  wraplength=QR_PX, font=("Microsoft YaHei UI", 9))\
            .pack(fill="x", pady=(4, 0))
        self.var_left = tk.StringVar(value="")

        bar = ttk.Frame(self); bar.pack(pady=4)
        self.btn_refresh = ttk.Button(bar, text="获取二维码", command=self.restart)
        self.btn_refresh.pack()

        self._started = False                # ★ 是否已发起过扫码；初始不自动拉码

    def switch_target(self, target):
        if target == self.target and not self._started:
            # 首次点单选但尚未获取过二维码：切换目标即可，等用户点「获取二维码」
            self.target = target
            return
        if target == self.target:
            return
        self.target = target
        self.restart()

    # ── 线程侧 ─────────────────────────────────────────
    def restart(self):
        self._started = True
        self._cancel = True
        self._gen += 1                       # ★ 旧线程作废，其投递的事件都会被丢弃
        if self._th and self._th.is_alive():
            self._th.join(timeout=3)
        self._cancel = False
        self.canvas.delete("qr")
        self.canvas.itemconfig("ph", text="正在获取二维码…")
        self.var_state.set("正在请求 xlogin / ptqrshow…")
        self.var_left.set("")
        self.btn_refresh.config(state="disabled", text="获取中…")
        gen = self._gen
        self._th = threading.Thread(target=self._run, args=(gen,), daemon=True)
        self._th.start()
        self.after(POLL_MS, self._poll)

    def _run(self, gen):
        def qr_ready(qr):
            self.q.put(("qr", gen, qr.png))
        def on_state(code, desc, left):
            self.q.put(("state", gen, code, desc, left))
        try:
            res = PTLogin(logger=lambda *a: None, target=self.target).run(
                qr_ready=qr_ready, on_state=on_state,
                should_stop=lambda: self._cancel or gen != self._gen)
            self.q.put(("done", gen, res, self.target))
        except QrExpired as e:
            self.q.put(("expired", gen, str(e)))
        except LoginError as e:
            self.q.put(("error", gen, str(e)))
        except Exception as e:
            self.q.put(("error", gen, f"{type(e).__name__}: {e}"))

    # ── 主线程侧 ───────────────────────────────────────
    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if not self._cancel:
            self.after(POLL_MS, self._poll)

    def _handle(self, ev):
        gen = ev[1]                          # ★ 事件带代际，旧代际直接丢弃
        if gen != self._gen:
            return
        kind = ev[0]
        if kind == "qr":
            self._show_qr(ev[2])
            self.var_state.set("请使用手机 QQ 扫码")
            self.btn_refresh.config(state="normal", text="重新获取二维码")
        elif kind == "state":
            code, desc, left = ev[2], ev[3], ev[4]
            self.var_state.set(f"[{code}] {desc}")
            self.var_left.set(f"剩余 {left} 秒" if left > 0 else "")
        elif kind == "expired":
            self.var_state.set("⏰ 二维码已过期，点击「重新获取」")
            self._dim()
            self.btn_refresh.config(state="normal", text="重新获取二维码")
        elif kind == "error":
            self.var_state.set("❌ 登录失败")
            self._dim()
            self.btn_refresh.config(state="normal", text="重新获取二维码")
            # ★ 不再自动 restart：否则旧线程被 cancel 抛出的错误会触发重启，
            #   造成「弹错 → 重启 → 又抛错」的无限循环。只提示手动重试。
            messagebox.showerror("登录失败", ev[2][:1200], parent=self)
        elif kind == "done":
            res, target = ev[2], ev[3]
            if target == "pay":
                self.var_state.set("正在兑换 Q币凭据…")
                try:
                    import pay as paymod
                    from store import update_pay
                    info = paymod.resolve_pay(res.pay_code, log=lambda *a: None)
                    update_pay(res.qq, info["openid"], info["access_token"],
                               info["refresh_token"],
                               int(time.time()) + info["expires_in"],
                               info["qb_balance"], nick=res.nick)
                    qb = info["qb_balance"] / 100.0
                    self.var_state.set(f"✅ 登录成功 QQ {res.qq}")
                    self._overlay_success(f"登录成功\nQ币 {qb:.2f}")
                    self.on_pay_ok(res.qq)
                    messagebox.showinfo(
                        "支付授权成功",
                        f"QQ {res.qq}  昵称 {res.nick}\n"
                        f"Q币余额：{qb:.2f}（{info['qb_balance']} Q点）",
                        parent=self)
                except Exception as e:
                    self.var_state.set("❌ 兑换失败")
                    messagebox.showerror("支付授权失败",
                                         f"扫码已成功，但兑换 Q币凭据失败：\n{e}",
                                         parent=self)
            else:
                self.var_state.set(f"✅ 登录成功 QQ {res.qq}")
                self._overlay_success(f"登录成功\nQQ {res.qq}")
                self.on_credit_ok(res)
                messagebox.showinfo(
                    "登录成功",
                    f"QQ {res.qq}\n昵称 {res.nick}\n"
                    f"凭据剩余 {days_left(res.exp_ts)} 天",
                    parent=self)

    def _show_qr(self, png):
        try:
            im = Image.open(BytesIO(png)).convert("RGB")
            im = im.resize((QR_PX, QR_PX), Image.NEAREST)
            self._img = ImageTk.PhotoImage(im)
            self.canvas.delete("ph")
            self.canvas.create_image(QR_PX // 2, QR_PX // 2, image=self._img, tags="qr")
        except Exception as e:
            self.var_state.set(f"二维码渲染失败: {e}")

    def _dim(self):
        self.canvas.create_rectangle(0, 0, QR_PX, QR_PX, fill="white",
                                     stipple="gray50", outline="", tags="qr")
        self.canvas.create_text(QR_PX // 2, QR_PX // 2, text="已过期\n点击「重新获取」",
                                fill="#333", font=("Microsoft YaHei UI", 11, "bold"),
                                justify="center", tags="qr")

    def _overlay_success(self, text="登录成功"):
        """扫码成功：在二维码上叠加半透明绿底 + 成功提示。"""
        self.canvas.delete("qr")              # 覆盖掉原二维码和旧遮罩
        self.canvas.create_rectangle(0, 0, QR_PX, QR_PX, fill="#2e7d32",
                                     stipple="gray25", outline="", tags="qr")
        self.canvas.create_text(QR_PX // 2, QR_PX // 2, text=text,
                                fill="#0a5c1a", font=("Microsoft YaHei UI", 14, "bold"),
                                justify="center", tags="qr")

    def cancel(self):
        self._cancel = True
        self._gen += 1                       # 作废所有在途事件
        if self._th and self._th.is_alive():
            self._th.join(timeout=2)


# ════════════════ 主窗口 ════════════════
class App:
    COLS = [("seq", "序号", 42), ("nick", "昵称", 100), ("qq", "QQ", 86),
            ("qb_prev", "上次", 94), ("qb", "Q币", 62),
            ("qb_change", "Q币变化", 72), ("score_prev", "上次", 94),
            ("score", "信用分", 64), ("score_change", "变化", 62)]
    TABLE_W = 676                      # COLS 各列宽度之和
    LEFT_W = TABLE_W + 20             # 为右侧滚动条预留空间

    def __init__(self, root):
        self.root = root
        root.title("腾讯游戏信用分 · 批量查询")
        root.geometry("868x740")
        # ★ 窗口居中，避免右侧按钮栏被屏幕底部截断
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = 868, min(740, sh - 40)
        root.geometry(f"{w}x{h}+{(sw - w)//2}+{max(0, (sh - h)//2 - 20)}")
        self.q = queue.Queue()
        self.busy = False
        self._build()
        self.reload_table()
        root.after(POLL_MS, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        try:
            self.scan.cancel()
        except Exception:
            pass
        self.root.destroy()

    def _build(self):
        main = tk.Frame(self.root); main.pack(fill="both", expand=True, padx=8, pady=8)

        # ── 左：表格 ──
        left = tk.Frame(main, width=self.LEFT_W)
        left.pack(side="left", fill="y", expand=False)
        left.pack_propagate(False)
        # ★ 真正的网格线：fieldbackground 用明显深灰作为行分隔线，斑马纹交替行色
        self._ts_style = ttk.Style()
        self._ts_style.configure("Grid.Treeview",
                                 rowheight=28, fieldbackground="#c0c0c0",
                                 borderwidth=0)
        self._ts_style.map("Grid.Treeview",
                           background=[("selected", "#cce4ff")],
                           foreground=[("selected", "#000000")])
        self.tree = ttk.Treeview(left, columns=[c[0] for c in self.COLS],
                                 show="headings", selectmode="extended",
                                 style="Grid.Treeview")
        for key, title, w in self.COLS:
            self.tree.heading(key, text=title,
                              command=lambda k=key: self.sort_by(k))
            anchor = "center" if key not in ("nick", "qq") else "w"
            self.tree.column(key, width=w, anchor=anchor, stretch=False)
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for st, fg in STATE_FG.items():
            self.tree.tag_configure(st, foreground=fg)
        # ★ 斑马纹 + 网格线：奇偶行交替底色，fieldbackground 灰线透出即真正分隔
        self.tree.tag_configure("odd",  background="#ffffff")
        self.tree.tag_configure("even", background="#f0f1f3")
        # ★ 昵称截断用字体；表格单元格左右各留少量内边距
        self._nick_font = tkfont.nametofont("TkDefaultFont")
        self.nick_full = {}                 # iid -> 完整昵称，供悬停提示使用
        self._tip = None                    # 当前悬浮提示窗口
        self._tip_iid = None
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self._hide_tip())
        # 滚动、排序、缩放列宽时收起提示，避免气泡停在错误位置
        self.tree.bind("<MouseWheel>", lambda e: self._hide_tip(), add="+")
        self.tree.bind("<Button-1>", lambda e: self._hide_tip(), add="+")
        self.tree.bind("<Configure>", lambda e: self._hide_tip(), add="+")

        # ── 右：扫码面板 + 操作按钮 ──
        right = tk.Frame(main, width=150); right.pack(side="right", fill="y", padx=(6, 0))
        right.pack_propagate(False)

        self.scan = ScanPanel(right, on_credit_ok=self._on_credit_ok,
                              on_pay_ok=self._on_pay_ok)
        self.scan.pack(fill="x", pady=(0, 8))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=4)

        # 账号汇总卡片：标题栏 + 彩色数值行
        SUM_BG, SUM_HEAD = "#f4f6f9", "#e6edf6"
        card = tk.Frame(right, bg=SUM_BG, highlightthickness=1,
                        highlightbackground="#d5dce6",
                        highlightcolor="#d5dce6")
        card.pack(fill="x", pady=(2, 0))
        tk.Label(card, text="账号汇总", bg=SUM_HEAD, fg="#2c3e50",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(fill="x")
        body = tk.Frame(card, bg=SUM_BG); body.pack(fill="x", padx=8, pady=5)

        self.var_sum_qq = tk.StringVar(value="0")
        self.var_sum_qb = tk.StringVar(value="0.00")
        self.var_sum_high = tk.StringVar(value="0")
        self.var_sum_mid = tk.StringVar(value="0")
        self.var_sum_low = tk.StringVar(value="0")
        for text, var, color in (("QQ总数", self.var_sum_qq, "#1e6fd9"),
                                 ("Q币总数", self.var_sum_qb, "#c47f00"),
                                 ("350分以上", self.var_sum_high, "#0a7d32"),
                                 ("151-349分", self.var_sum_mid, "#d97706"),
                                 ("150分以下", self.var_sum_low, "#c62828")):
            row = tk.Frame(body, bg=SUM_BG); row.pack(fill="x", pady=1)
            tk.Label(row, text=text, bg=SUM_BG, fg="#5a6472",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Label(row, textvariable=var, bg=SUM_BG, fg=color,
                     font=("Microsoft YaHei UI", 10, "bold")).pack(side="right")
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)

        # 操作按钮（每组两个按钮横向排列）
        btns = tk.Frame(right); btns.pack(fill="x", pady=4)
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        self.btn_score = ttk.Button(btns, text="查分",
                                    command=lambda: self.refresh("all"))
        self.btn_score.grid(row=0, column=0, sticky="ew", padx=(0, 2), pady=2)
        self.btn_qb_all = ttk.Button(btns, text="查币",
                                     command=lambda: self.refresh_qb("all"))
        self.btn_qb_all.grid(row=0, column=1, sticky="ew", padx=(2, 0), pady=2)

        ttk.Separator(btns, orient="horizontal").grid(row=1, column=0,
                                                       columnspan=2, sticky="ew", pady=6)
        ttk.Button(btns, text="导出 CSV",
                   command=self.export_csv).grid(row=2, column=0, sticky="ew",
                                                 padx=(0, 2), pady=2)
        ttk.Button(btns, text="导入 CSV",
                   command=self.import_csv).grid(row=2, column=1, sticky="ew",
                                                 padx=(2, 0), pady=2)

        ttk.Separator(btns, orient="horizontal").grid(row=3, column=0,
                                                       columnspan=2, sticky="ew", pady=6)
        ttk.Label(btns, text="并发:").grid(row=4, column=0, sticky="w")
        self.sp_workers = ttk.Spinbox(btns, from_=1, to=5, width=6)
        self.sp_workers.set(3); self.sp_workers.grid(row=4, column=1, sticky="ew",
                                                     pady=2)
        ttk.Button(btns, text="删除选中",
                   command=self.del_selected).grid(row=5, column=0,
                                                   columnspan=2, sticky="ew", pady=2)

        # ── 底部：日志区 ──
        bot = ttk.Frame(self.root); bot.pack(fill="x", padx=8, pady=(0, 6))
        self.txt_log = tk.Text(bot, height=6, state="disabled", wrap="word",
                               font=("Microsoft YaHei UI", 9), relief="flat",
                               background="#f7f7f7", highlightthickness=1,
                               highlightbackground="#d0d0d0")
        sb = ttk.Scrollbar(bot, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._log("就绪 · 共 0 个账号")

    # ── 日志 ───────────────────────────────────────────
    def _log(self, msg):
        """往底部日志区追加一行带时间戳的日志。message 无效时静默丢弃。"""
        ts = time.strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{ts}] {msg}\n")
        # ★ 只保留最近 200 行，避免无限增长
        if int(self.txt_log.index("end-1c").split(".")[0]) > 200:
            self.txt_log.delete("1.0", "2.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # ── 数据 ───────────────────────────────────────────
    def reload_table(self):
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        self.nick_full = {}
        for idx, a in enumerate(all_accounts()):
            qb = a.get("qb", 0)
            has_qb = bool(a.get("pay_token"))
            qb_txt = self._format_qb(qb) if has_qb and a.get("qb_mtime") else "-"
            qb_change = self._format_change(qb, a.get("qb_prev"), 100) if has_qb else "-"
            qb_prev_txt = (self._format_qb(a.get("qb_prev"))
                           if has_qb and a.get("qb_mtime") else "-")
            # ★ 有真实数据才显示：mtime 或分数非 0；查询失败时显示 - 而不是 0
            has_score = bool(a["query_at"]) and (a["mtime"] > 0 or a["score"] > 0)
            score_txt = self._format_score(a["score"]) if has_score else "-"
            score_prev_txt = (self._format_score(a.get("score_prev"))
                              if has_score else "-")
            score_change = self._format_change(a["score"], a.get("score_prev"), 1)
            full_nick = a["nick"] or "-"
            vals = [idx + 1, self._ellipsize(full_nick), a["qq"],
                    qb_prev_txt, qb_txt, qb_change,
                    score_prev_txt, score_txt, score_change]
            tag = a["state"] if a["state"] in STATE_FG else "never"
            zebra = "even" if idx % 2 else "odd"
            iid = self.tree.insert("", "end", values=vals, tags=(tag, zebra))
            self.rows[iid] = a["qq"]
            self.nick_full[iid] = full_nick
        self._update_summary()
        self._log(f"已加载 {len(self.rows)} 个账号")

    def _ellipsize(self, text):
        """按像素宽度截断文本并加省略号；放得下则原样返回。"""
        if not text or text == "-":
            return text
        fit = self.tree.column("nick", "width") - 8
        if self._nick_font.measure(text) <= fit:
            return text
        ell_w = self._nick_font.measure(ELLIPSIS)
        budget = fit - ell_w
        if budget <= 0:
            return ELLIPSIS
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._nick_font.measure(text[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + ELLIPSIS

    # ── 昵称悬停提示 ───────────────────────────────────
    def _on_tree_motion(self, event):
        iid = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not iid or col != "#2":          # 第2列 = 昵称
            self._hide_tip()
            return
        full = self.nick_full.get(iid)
        if not full or self._nick_font.measure(full) <= (self.tree.column("nick", "width") - 8):
            self._hide_tip()               # 完整显示时不弹提示
            return
        if self._tip_iid == iid and self._tip and self._tip.winfo_exists():
            return
        self._hide_tip()
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=full, background="#ffffe0",
                 relief="solid", borderwidth=1,
                 font=("Microsoft YaHei UI", 9), justify="left",
                 padx=6, pady=3).pack()
        x = self.tree.winfo_rootx() + event.x + 14
        y = self.tree.winfo_rooty() + event.y + 18
        tip.wm_geometry(f"+{x}+{y}")
        self._tip, self._tip_iid = tip, iid

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
        self._tip, self._tip_iid = None, None

    @staticmethod
    def _format_qb(value):
        """Q点整数 -> Q币字符串；无历史值显示 -。"""
        return f"{value / 100.0:.2f}" if value is not None else "-"

    @staticmethod
    def _format_score(value):
        """信用分整数；无历史值显示 -。"""
        return str(value) if value is not None else "-"

    @staticmethod
    def _format_change(current, previous, divisor):
        if previous is None:
            return "-"
        delta = (current - previous) / divisor
        if divisor == 100:
            return f"{delta:+.2f}"
        return f"{delta:+.0f}"

    def _update_summary(self):
        """刷新右侧账号汇总：QQ数、Q币总额、350分以上、151-349分、150分以下。"""
        accs = all_accounts()
        qb_total = sum((a.get("qb", 0) or 0) for a in accs
                       if a.get("pay_openid") and a.get("pay_token"))
        # 只统计有真实分数的账号，三档区间连续覆盖：<=150 / 151-349 / >=350
        scored = [a["score"] for a in accs
                  if a.get("query_at") and (a["mtime"] > 0 or a["score"] > 0)]
        low = sum(1 for s in scored if s <= 150)
        mid = sum(1 for s in scored if 151 <= s <= 349)
        high = sum(1 for s in scored if s >= 350)
        self.var_sum_qq.set(str(len(accs)))
        self.var_sum_qb.set(f"{qb_total / 100.0:.2f}")
        self.var_sum_high.set(str(high))
        self.var_sum_mid.set(str(mid))
        self.var_sum_low.set(str(low))

    def sort_by(self, key):
        idx = [c[0] for c in self.COLS].index(key)
        items = [(self.tree.set(i, key), i) for i in self.tree.get_children()]
        try:
            items.sort(key=lambda x: float(x[0].replace("天", "").replace("-", "0")))
        except ValueError:
            items.sort(key=lambda x: x[0])
        for pos, (_, iid) in enumerate(items):
            self.tree.move(iid, "", pos)

    # ── 事件队列（★ 工作线程绝不直接改控件）────────────
    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    def _handle(self, ev):
        k = ev[0]
        if k == "row":
            qq, res, err = ev[1], ev[2], ev[3]
            self._paint_row(qq, res, err)
        elif k == "qb_row":
            qq, bal, err = ev[1], ev[2], ev[3]
            self._paint_qb_row(qq, bal, err)
        elif k == "prog":
            pass   # 进度条已移除，进度信息由日志承担
        elif k == "finish":
            ok, fail, exp = ev[1], ev[2], ev[3]
            self.busy = False
            self._set_buttons(True)
            self._update_summary()
            self._log(f"信用分刷新完成 · 成功 {ok} / 失败 {fail} / 凭据过期 {exp}")
        elif k == "finish_qb":
            ok, fail = ev[1], ev[2]
            self.busy = False
            self._set_buttons(True)
            self._update_summary()
            self._log(f"Q币刷新完成 · 成功 {ok} / 失败 {fail}")
        elif k == "login_ok":
            self.reload_table()
            self._log(f"已添加账号 {ev[1]}")

    def _paint_row(self, qq, res, err):
        iid = next((i for i, v in self.rows.items() if v == qq), None)
        if not iid:
            self.reload_table(); return
        if res and res.ok:
            st, fg = "ok", STATE_FG["ok"]
            acc = next((a for a in all_accounts() if a["qq"] == qq), None)
            self.tree.set(iid, "score", self._format_score(res.score))
            self.tree.set(iid, "score_prev",
                          self._format_score(acc.get("score_prev")) if acc else "-")
            self.tree.set(iid, "score_change",
                          self._format_change(res.score, acc.get("score_prev"), 1)
                          if acc else "-")
            self._update_summary()
        else:
            expired = bool(res and res.expired)
            st = "expired" if expired else "error"
        zebra = "even" if "even" in self.tree.item(iid, "tags") else "odd"
        self.tree.item(iid, tags=(st, zebra))

    def _paint_qb_row(self, qq, bal, err):
        iid = next((i for i, v in self.rows.items() if v == qq), None)
        if not iid:
            self.reload_table(); return
        if bal is not None:
            acc = next((a for a in all_accounts() if a["qq"] == qq), None)
            self.tree.set(iid, "qb", self._format_qb(bal))
            self.tree.set(iid, "qb_prev",
                          self._format_qb(acc.get("qb_prev")) if acc else "-")
            self.tree.set(iid, "qb_change",
                          self._format_change(bal, acc.get("qb_prev"), 100)
                          if acc else "-")
        else:
            pass

    def _set_buttons(self, on):
        st = "normal" if on else "disabled"
        for b in (self.btn_score, self.btn_qb_all):
            b.config(state=st)

    # ── 动作 ───────────────────────────────────────────
    def _on_credit_ok(self, res):
        upsert(res)
        self.q.put(("login_ok", res.qq))
        # ★ 登录只拿到凭据、还没有分数；后台立即补查一次，
        #   否则表格里该账号的信用分会一直显示 -，直到手动点「查分」。
        threading.Thread(target=self._query_credit_after_login,
                         args=(res,), daemon=True).start()

    def _query_credit_after_login(self, res):
        """扫码登录成功后补查一次信用分，结果写库并刷新对应行。"""
        try:
            r = query_credit(res.gs_id, res.gs_code)
        except Exception as e:
            self.q.put(("row", res.qq, None, f"{type(e).__name__}: {e}"))
            return
        if r.ok:
            update_result(res.qq, r, "ok")
            self.q.put(("row", res.qq, r, ""))
        else:
            state = "expired" if r.expired else "error"
            update_result(res.qq, _EMPTY, state, r.reason)
            self.q.put(("row", res.qq, r, r.reason))

    def _on_pay_ok(self, qq):
        self.q.put(("login_ok", qq))

    def refresh(self, mode):
        if self.busy:
            messagebox.showinfo("提示", "已有任务在运行"); return
        accs = all_accounts()
        # 纯支付授权账号没有信用分凭据，查分时跳过，避免白发请求
        accs = [a for a in accs if a.get("gs_code")]
        if not accs:
            messagebox.showinfo("提示", "请先添加账号"); return

        if mode == "selected":
            sel = {self.rows[i] for i in self.tree.selection() if i in self.rows}
            accs = [a for a in accs if a["qq"] in sel]
            if not accs:
                messagebox.showinfo("提示", "请先在列表中选择账号"); return
        elif mode == "today":
            tb = time.localtime()
            t0 = int(time.mktime((tb.tm_year, tb.tm_mon, tb.tm_mday, 0, 0, 0, 0, 0, -1)))
            # ★ 「今天是否刷过」看本地查询时刻 query_at，而非分数数据时间 mtime
            #   （信用分一天只变一次，mtime 会停留在昨天，导致误判）
            accs = [a for a in accs if a["query_at"] < t0]
            if not accs:
                messagebox.showinfo("提示", "今天所有账号都已查询过"); return
        else:
            # 全部：仍先按 exp 剔除已过期，一个包都不发
            now = time.time()
            dead = [a["qq"] for a in accs if jwt_exp(a["gs_code"]) and
                    jwt_exp(a["gs_code"]) <= now]
            if dead:
                if not messagebox.askyesno(
                        "凭据过期",
                        f"有 {len(dead)} 个账号凭据已过期（需重新扫码）。\n"
                        f"跳过它们并继续刷新其余账号？"):
                    return
                accs = [a for a in accs if a["qq"] not in dead]
                for qq in dead:
                    update_result(qq, _EMPTY, "expired", "凭据过期，需重新扫码")

        self.busy = True
        self._set_buttons(False)
        n = max(1, min(5, int(self.sp_workers.get() or 3)))
        self._log(f"开始刷新信用分 {len(accs)} 个账号（并发 {n}）")
        threading.Thread(target=self._run_refresh, args=(accs, n), daemon=True).start()

    def _run_refresh(self, accs, n):
        stat = {"ok": 0, "fail": 0, "exp": 0}
        def on_res(acc, res, err):
            if res and res.ok: stat["ok"] += 1
            elif res and res.expired: stat["exp"] += 1
            else: stat["fail"] += 1
            state = "ok" if (res and res.ok) else ("expired" if (res and res.expired) else "error")
            update_result(acc["qq"], res or _EMPTY, state, err or "")
            self.q.put(("row", acc["qq"], res, err))
        try:
            Scheduler(workers=n).refresh(accs, on_res,
                                         lambda d, t: self.q.put(("prog", d, t)))
        finally:
            self.q.put(("finish", stat["ok"], stat["fail"], stat["exp"]))

    # ── Q币刷新 ───────────────────────────────────────
    def refresh_qb(self, mode):
        if self.busy:
            messagebox.showinfo("提示", "已有任务在运行"); return
        accs = all_accounts()
        # 只刷有 pay_openid + pay_token 的账号
        accs = [a for a in accs if a.get("pay_openid") and a.get("pay_token")]
        if not accs:
            messagebox.showinfo("提示", "暂无已授权 Q币的账号（先用「支付」扫码授权）"); return

        if mode == "selected":
            sel = {self.rows[i] for i in self.tree.selection() if i in self.rows}
            accs = [a for a in accs if a["qq"] in sel]
            if not accs:
                messagebox.showinfo("提示", "请先在列表中选择已授权的账号"); return

        self.busy = True
        self._set_buttons(False)
        n = max(1, min(5, int(self.sp_workers.get() or 3)))
        self._log(f"开始刷新 Q币 {len(accs)} 个账号（并发 {n}）")
        threading.Thread(target=self._run_refresh_qb, args=(accs, n), daemon=True).start()

    def _run_refresh_qb(self, accs, n):
        from pay import refresh_qb as _refresh_qb, PayError
        from store import update_pay
        stat = {"ok": 0, "fail": 0}
        total = len(accs)
        for i, a in enumerate(accs):
            try:
                bal = _refresh_qb(a["pay_openid"], a["pay_token"])
                update_pay(a["qq"], a["pay_openid"], a["pay_token"],
                           a.get("pay_refresh", ""), a.get("pay_exp_ts", 0),
                           bal["qb_balance"])
                stat["ok"] += 1
                self.q.put(("qb_row", a["qq"], bal["qb_balance"], ""))
            except PayError as e:
                stat["fail"] += 1
                self.q.put(("qb_row", a["qq"], None, str(e)[:60]))
            except Exception as e:
                stat["fail"] += 1
                self.q.put(("qb_row", a["qq"], None, f"{type(e).__name__}: {e}"[:60]))
            self.q.put(("prog", i + 1, total))
        self.q.put(("finish_qb", stat["ok"], stat["fail"]))

    def del_selected(self):
        sel = [self.rows[i] for i in self.tree.selection() if i in self.rows]
        if not sel: return
        if not messagebox.askyesno("确认", f"删除 {len(sel)} 个账号？\n{', '.join(sel[:8])}"):
            return
        for qq in sel: delete(qq)
        self.reload_table()

    def export_csv(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile="gamecredit.csv",
            filetypes=[("CSV", "*.csv")])
        if not p: return
        # ★ utf-8-sig，Excel 才能正确识别中文
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["QQ号", "昵称", "信用分", "Q币余额", "凭据剩余天数", "凭据到期",
                        "状态", "分数更新时间", "最近查询", "备注"])
            for a in all_accounts():
                exp = jwt_exp(a["gs_code"])
                qb = a.get("qb", 0)
                qb_txt = f"{qb / 100.0:.2f}" if (a.get("pay_token") and qb) else ""
                w.writerow([a["qq"], a["nick"], a["score"], qb_txt, days_left(exp),
                            time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "",
                            STATE_TXT.get(a["state"], a["state"]),
                            time.strftime("%Y-%m-%d %H:%M", time.localtime(a["mtime"])) if a["mtime"] else "",
                            time.strftime("%Y-%m-%d %H:%M", time.localtime(a["query_at"])) if a["query_at"] else "",
                            a.get("note", "")])
        messagebox.showinfo("导出完成", p)

    def import_csv(self):
        """从 CSV 导入账号：按 QQ号 匹配，新增或就地更新，不动已有凭据。"""
        from store import import_account
        p = filedialog.askopenfilename(
            title="选择要导入的 CSV", filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")])
        if not p:
            return
        try:
            with open(p, "r", newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
        except UnicodeDecodeError:
            try:
                with open(p, "r", newline="", encoding="gbk") as f:
                    rows = list(csv.DictReader(f))
            except Exception as e:
                messagebox.showerror("导入失败", f"无法读取文件：{e}")
                return
        except Exception as e:
            messagebox.showerror("导入失败", f"无法读取文件：{e}")
            return

        if not rows:
            messagebox.showinfo("提示", "CSV 没有数据行")
            return

        def pick(row, *names):
            for n in names:
                if n in row and str(row[n]).strip() != "":
                    return str(row[n]).strip()
            return None

        def to_int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        added = updated = skipped = 0
        for row in rows:
            qq = pick(row, "QQ号", "QQ", "qq")
            if not qq:
                skipped += 1
                continue
            score = to_int(pick(row, "信用分", "score"))
            qb = pick(row, "Q币余额", "Q币", "qb")
            try:
                qb = float(qb) if qb not in (None, "") else None
            except ValueError:
                qb = None
            mtime = to_int(pick(row, "分数更新时间", "mtime"))
            if mtime:
                # CSV 里是可读时间，这里尝试转成时间戳
                for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
                    try:
                        mtime = int(time.mktime(time.strptime(mtime, fmt)))
                        break
                    except ValueError:
                        mtime = None
            res = import_account(
                qq=qq,
                nick=pick(row, "昵称", "nick"),
                gs_id=pick(row, "gs_id", "gsId"),
                gs_code=pick(row, "gs_code", "gsCode"),
                score=score,
                qb=qb,
                note=pick(row, "备注", "note"),
                mtime=mtime)
            if res == "added":
                added += 1
            elif res == "updated":
                updated += 1
            else:
                skipped += 1

        self.reload_table()
        messagebox.showinfo(
            "导入完成",
            f"新增 {added} 个，更新 {updated} 个，跳过 {skipped} 个。\n"
            f"来源：{p}")


class _EmptyRes:
    ok = False; score = 0; rank = 0.0; stars = 0; mtime = 0
    reason = ""; expired = False
_EMPTY = _EmptyRes()


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")     # Windows 原生观感
    except Exception:
        pass
    App(root)
    root.mainloop()
