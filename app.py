"""app.py — OpenGameCredit v2.0.0 图形界面

   启动: python app.py

   v2 变化：
     · 一码两查：扫一次码，同时授权 信用分 / Q币 / 黑白
     · 卡片式浅色仪表盘布局，窗口定尺寸后锁定不可拉伸
     · 新增「黑白」列与「查黑白」按钮（依据活动接口应答判定）
"""
import csv, queue, threading, time, tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont
from io import BytesIO
from PIL import Image, ImageTk

from login_oauth import OAuthPTLogin, LoginError, QrExpired
from query import query_credit, query_credit_token
from store import (all_accounts, update_result, update_bw, update_oauth,
                   update_qb_value, ensure_account, delete, APP_DIR)
from bw import check as bw_check, WHITE, BLACK, DEAD
from worker import Scheduler
from utils import jwt_exp, days_left

THEME = "clam"          # vista 主题无法自定义配色，换 clam 才能做出扁平卡片风

QR_PX = 148             # 二维码显示边长（NEAREST 放大保持锐利）
POLL_MS = 60            # UI 事件队列轮询间隔
APP_VERSION = "2.0.0"
ELLIPSIS = "..."

# ── 设计令牌 ───────────────────────────────────────────
FONT   = "Microsoft YaHei UI"
BG     = "#f3f5f9"      # 应用底色
CARD   = "#ffffff"      # 卡片
BORDER = "#e4e7ec"      # 卡片描边
SURF   = "#f7f9fc"      # 浅色底（未选中、输入框）
TEXT   = "#101828"      # 主文字
SUB    = "#475467"      # 次级文字
MUTED  = "#98a2b3"      # 弱化文字
PRIMARY = "#2f6fed"
OK_C   = "#067647"
WARN_C = "#b54708"
DANGER_C = "#d92d20"

STATE_TXT = {"ok": "正常", "expired": "凭据过期", "error": "查询失败", "never": "未查询"}
# 正常行保持深灰（不喧哗），异常行才上色
STATE_FG = {"ok": TEXT, "expired": DANGER_C, "error": WARN_C, "never": MUTED}


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """在 canvas 上画圆角矩形（用平滑多边形近似，兼容所有 Tk 版本）。"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1,
           x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2,
           x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


# ════════════════ 基础控件 ════════════════
class PillButton(tk.Canvas):
    """圆角按钮。支持 config(text/state/kind)，与 ttk.Button 用法兼容。"""

    KINDS = {
        "primary": dict(fill=PRIMARY,  hover="#2a63d6", press="#2455bb", fg="#ffffff", border=""),
        "soft":    dict(fill="#eef4ff", hover="#e3edff", press="#d6e4ff", fg=PRIMARY,  border=""),
        "ghost":   dict(fill="#ffffff", hover="#f5f8fd", press="#eaf0f9", fg="#344054", border="#d8e0ea"),
        "plain":   dict(fill="#ffffff", hover="#f5f8fd", press="#eaf0f9", fg=SUB,      border=""),
        "danger":  dict(fill="#fef3f2", hover="#fee4e2", press="#fdd8d4", fg=DANGER_C, border=""),
    }

    def __init__(self, master, text="", command=None, kind="ghost", height=32,
                 width=None, radius=8, font=None, bg=CARD):
        tk.Canvas.__init__(self, master, height=height, highlightthickness=0, bd=0,
                           bg=bg, cursor="hand2")
        if width:
            self.configure(width=width)
        self.text, self.command, self.kind = text, command, kind
        self.height, self.radius = height, radius
        self.font = font or (FONT, 9, "bold")
        self._state, self._hover, self._press = "normal", False, False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._down)
        self.bind("<ButtonRelease-1>", self._up)
        self._draw()

    # ── 绘制 ──
    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 16)
        h = self.height
        st = self.KINDS.get(self.kind, self.KINDS["ghost"])
        if self._state == "disabled":
            fill, fg, border = SURF, "#b6bfcc", BORDER
        else:
            fill, fg, border = st["fill"], st["fg"], st["border"]
            if self._press:
                fill = st["press"]
            elif self._hover:
                fill = st["hover"]
        if border:
            round_rect(self, .5, .5, w - .5, h - .5, self.radius, fill=border, outline="")
            round_rect(self, 1.5, 1.5, w - 1.5, h - 1.5, max(1, self.radius - 1),
                       fill=fill, outline="")
        else:
            round_rect(self, 0, 0, w, h, self.radius, fill=fill, outline="")
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)

    # ── 交互 ──
    def _enter(self, _):
        self._hover = True
        self._draw()

    def _leave(self, _):
        self._hover = self._press = False
        self._draw()

    def _down(self, _):
        if self._state == "disabled":
            return
        self._press = True
        self._draw()

    def _up(self, e):
        if self._state == "disabled":
            return
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.height
        self._press = False
        self._draw()
        if inside and self.command:
            self.command()

    def config(self, **kw):
        redraw = False
        for k, attr in (("text", "text"), ("state", "_state"), ("kind", "kind")):
            if k in kw:
                setattr(self, attr, kw.pop(k))
                redraw = True
        if "command" in kw:
            self.command = kw.pop("command")
        if kw:
            tk.Canvas.config(self, **kw)
        if redraw:
            self._draw()

    configure = config


class Segmented(tk.Canvas):
    """胶囊式分段选择器（替代原来的圆点单选）。"""

    def __init__(self, master, items, value=None, command=None, height=30, bg=CARD):
        tk.Canvas.__init__(self, master, height=height, highlightthickness=0, bd=0,
                           bg=bg, cursor="hand2")
        self.items = list(items)
        self.value = value if value is not None else self.items[0][0]
        self.command = command
        self.height = height
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 40)
        h = self.height
        n = max(1, len(self.items))
        seg = w / n
        round_rect(self, 0, 0, w, h, h / 2, fill="#eef1f6", outline="")
        for i, (val, label) in enumerate(self.items):
            x1 = i * seg
            active = val == self.value
            if active:
                round_rect(self, x1 + 2, 2, x1 + seg - 2, h - 2, (h - 4) / 2,
                           fill="#ffffff", outline=BORDER)
            self.create_text(x1 + seg / 2, h / 2,
                             text=label, fill=TEXT if active else SUB,
                             font=(FONT, 9, "bold" if active else "normal"))

    def set(self, value):
        if value != self.value:
            self.value = value
            self._draw()

    def _click(self, e):
        n = max(1, len(self.items))
        seg = max(self.winfo_width(), 40) / n
        idx = min(n - 1, max(0, int(e.x // seg)))
        val = self.items[idx][0]
        if val != self.value:
            self.set(val)
            if self.command:
                self.command(val)


class StatusChip(tk.Canvas):
    """顶栏状态胶囊：小圆点 + 文案。"""

    KINDS = {"ok": ("#ecfdf3", OK_C, "#12b76a"),
             "busy": ("#eef4ff", PRIMARY, PRIMARY),
             "warn": ("#fffaeb", WARN_C, "#f79009"),
             "err": ("#fef3f2", DANGER_C, "#f04438")}

    def __init__(self, master, text="就绪", kind="ok", bg=CARD):
        tk.Canvas.__init__(self, master, height=26, highlightthickness=0, bd=0, bg=bg)
        self.text, self.kind = text, kind
        # ★ 名字不能叫 _w：Canvas 内部用 _w 存控件路径名，覆盖后所有绘制都会炸
        self._dw = None                     # 也不回读 canvas 的 width 选项：
                                            #   Tk 9.0 下它返回 '10c' 这类带单位字符串
        self._font = tkfont.Font(family=FONT, size=9, weight="bold")
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self._font.measure(self.text) + 36
        fill, fg, dot = self.KINDS.get(self.kind, self.KINDS["ok"])
        round_rect(self, 0, 0, w, 26, 13, fill=fill, outline="")
        self.create_oval(11, 11, 17, 17, fill=dot, outline="")
        self.create_text(22, 13, text=self.text, anchor="w", fill=fg, font=self._font)
        if self._dw != w:
            self._dw = w
            self.configure(width=w)

    def set(self, text, kind="ok"):
        self.text, self.kind = text, kind
        self._draw()


class DistBar(tk.Canvas):
    """分数分布条：三段胶囊按比例铺开。"""

    def __init__(self, master, height=8, bg=CARD):
        tk.Canvas.__init__(self, master, height=height, highlightthickness=0, bd=0, bg=bg)
        self.height = height
        self.data = [(0, "#12b76a"), (0, "#f79009"), (0, "#f04438")]
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, high, mid, low):
        self.data = [(high, "#12b76a"), (mid, "#f79009"), (low, "#f04438")]
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 20)
        h = self.height
        round_rect(self, 0, 0, w, h, h / 2, fill="#eef1f6", outline="")
        total = sum(v for v, _ in self.data)
        if total <= 0:
            return
        gap = 2
        x = 0.0
        for val, color in self.data:
            if val <= 0:
                continue
            seg_w = max(h, (w - gap * 2) * val / total)
            x2 = min(w, x + seg_w)
            if x2 - x < 4:
                x = x2
                continue
            round_rect(self, x, 0, x2, h, h / 2, fill=color, outline="")
            x = x2 + gap
            if x >= w:
                break


class Stepper(tk.Frame):
    """并发数步进器（替代 ttk.Spinbox）。"""

    def __init__(self, master, var, lo=1, hi=5, bg=CARD):
        super().__init__(master, bg=bg)
        self.var, self.lo, self.hi = var, lo, hi
        PillButton(self, "−", command=lambda: self._step(-1), kind="ghost",
                   height=26, width=26, radius=7, font=(FONT, 12, "bold"), bg=bg)\
            .pack(side="left")
        tk.Label(self, textvariable=var, bg=bg, fg=TEXT, width=3,
                 font=(FONT, 10, "bold")).pack(side="left")
        PillButton(self, "+", command=lambda: self._step(1), kind="ghost",
                   height=26, width=26, radius=7, font=(FONT, 12, "bold"), bg=bg)\
            .pack(side="left")

    def _step(self, delta):
        try:
            cur = int(self.var.get())
        except Exception:
            cur = 3
        self.var.set(max(self.lo, min(self.hi, cur + delta)))


class SearchBox(tk.Frame):
    """带放大镜与占位提示的搜索框，直接绑定 StringVar。"""

    def __init__(self, master, var, chars=13, bg=CARD, placeholder="搜索昵称 / QQ"):
        super().__init__(master, bg=bg)
        self.var = var
        box = tk.Frame(self, bg=SURF, highlightthickness=1,
                       highlightbackground=BORDER, highlightcolor=PRIMARY)
        box.pack()
        tk.Label(box, text="⌕", bg=SURF, fg=MUTED, font=(FONT, 12)).pack(side="left",
                                                                          padx=(8, 0))
        self.entry = tk.Entry(box, textvariable=var, relief="flat", bd=0,
                              bg=SURF, fg=TEXT, insertbackground=TEXT,
                              font=(FONT, 9), highlightthickness=0, width=chars)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(5, 8))
        self._ph = tk.Label(box, text=placeholder, bg=SURF, fg=MUTED, font=(FONT, 9))
        self._ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        var.trace_add("write", lambda *a: self._sync())
        self._sync()

    def _sync(self):
        if self.var.get():
            self._ph.place_forget()
        else:
            self._ph.place(in_=self.entry, x=1, rely=.5, anchor="w")


# ════════════════ 扫码登录面板（内嵌，非弹窗） ════════════════
class ScanPanel(tk.Frame):
    """扫码区：一个二维码就够（信用分 / Q币 / 黑白 共用一套凭据）。"""

    def __init__(self, master, on_login):
        super().__init__(master, bg=CARD)
        self.on_login = on_login             # 登录成功回调(OAuthResult)
        self.q = queue.Queue()
        self._cancel = False
        self._img = None
        self._th = None
        self._gen = 0                        # ★ 代际标记：每次重启 +1，旧线程事件作废
        self._started = False                # ★ 初始不自动拉码

        # 单码登录：一个二维码同时授权信用分 / Q币 / 黑白，不再分「信用分｜支付」
        tk.Label(self, text="扫码一次 · 同时授权信用分 / Q币 / 黑白",
                 bg=CARD, fg=MUTED, font=(FONT, 8)).pack(fill="x", pady=(0, 2))

        # 二维码容器：浅底 + 描边，二维码居中
        holder = tk.Frame(self, bg=SURF, highlightthickness=1,
                          highlightbackground=BORDER)
        holder.pack(pady=(8, 0))
        self.canvas = tk.Canvas(holder, width=QR_PX, height=QR_PX, bg="#ffffff",
                                highlightthickness=0, bd=0)
        self.canvas.pack(padx=9, pady=9)
        self.canvas.create_text(QR_PX // 2, QR_PX // 2,
                                text="点击下方按钮\n获取二维码",
                                fill=MUTED, font=(FONT, 9), justify="center", tags="ph")

        self.var_state = tk.StringVar(value="就绪 · 尚未获取二维码")
        self.var_left = tk.StringVar(value="")
        tk.Label(self, textvariable=self.var_state, bg=CARD, fg=SUB, height=2,
                 wraplength=222, justify="center", font=(FONT, 9))\
            .pack(fill="x", pady=(8, 0))

        self.btn_refresh = PillButton(self, "获取二维码", command=self.restart,
                                      kind="primary", height=32)
        self.btn_refresh.pack(fill="x", pady=(6, 0))

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
        self.var_state.set("正在请求 ptqrshow…")
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
            res = OAuthPTLogin(logger=lambda *a: None).run(
                qr_ready=qr_ready, on_state=on_state,
                should_stop=lambda: self._cancel or gen != self._gen)
            self.q.put(("done", gen, res))
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
            tail = f"  ·  剩余 {left}s" if left > 0 else ""
            self.var_state.set(f"[{code}] {desc}{tail}")
        elif kind == "expired":
            self.var_state.set("二维码已过期，请重新获取")
            self._dim()
            self.btn_refresh.config(state="normal", text="重新获取二维码")
        elif kind == "error":
            self.var_state.set("登录失败，请重试")
            self._dim()
            self.btn_refresh.config(state="normal", text="重新获取二维码")
            # ★ 不再自动 restart：否则旧线程被 cancel 抛出的错误会触发重启，
            #   造成「弹错 → 重启 → 又抛错」的无限循环。只提示手动重试。
            messagebox.showerror("登录失败", ev[2][:1200], parent=self)
        elif kind == "done":
            res = ev[2]                      # OAuthResult：一次扫码的全套凭据
            self.var_state.set(f"登录成功 · QQ {res.qq}")
            self._overlay_success(f"登录成功\nQQ {res.qq}")
            self.btn_refresh.config(state="normal", text="重新获取二维码")
            self.on_login(res)

    def _show_qr(self, png):
        try:
            im = Image.open(BytesIO(png)).convert("RGB")
            im = im.resize((QR_PX, QR_PX), Image.NEAREST)
            self._img = ImageTk.PhotoImage(im)
            self.canvas.delete("ph")
            self.canvas.delete("qr")
            self.canvas.create_image(QR_PX // 2, QR_PX // 2, image=self._img, tags="qr")
        except Exception as e:
            self.var_state.set(f"二维码渲染失败: {e}")

    def _dim(self):
        self.canvas.delete("qr")
        self.canvas.create_rectangle(0, 0, QR_PX, QR_PX, fill="#ffffff",
                                     stipple="gray50", outline="", tags="qr")
        self.canvas.create_text(QR_PX // 2, QR_PX // 2, text="已过期\n请重新获取",
                                fill=SUB, font=(FONT, 10, "bold"),
                                justify="center", tags="qr")

    def _overlay_success(self, text="登录成功"):
        """扫码成功：在二维码上叠加半透明绿底 + 成功提示。"""
        self.canvas.delete("qr")              # 覆盖掉原二维码和旧遮罩
        self.canvas.create_rectangle(0, 0, QR_PX, QR_PX, fill="#12b76a",
                                     stipple="gray25", outline="", tags="qr")
        self.canvas.create_text(QR_PX // 2, QR_PX // 2, text=text,
                                fill="#054f31", font=(FONT, 12, "bold"),
                                justify="center", tags="qr")

    def cancel(self):
        self._cancel = True
        self._gen += 1                       # 作废所有在途事件
        if self._th and self._th.is_alive():
            self._th.join(timeout=2)


# ════════════════ 主窗口 ════════════════
class App:
    COLS = [("seq", "#", 32), ("nick", "昵称", 92), ("qq", "QQ", 98),
            ("state", "状态", 66), ("bw", "黑白", 48),
            ("qb_prev", "上次Q币", 68), ("qb", "Q币", 60),
            ("qb_change", "变动", 54), ("score_prev", "上次分数", 70),
            ("score", "信用分", 60), ("score_change", "变动", 54)]
    RIGHT_W = 268                      # 右栏宽度
    PREF_W, PREF_H = 1084, 880         # 理想尺寸（按屏幕再收一次）

    def __init__(self, root):
        self.root = root
        root.title("腾讯游戏信用分 · 批量查询")
        root.configure(bg=BG)
        root.withdraw()                # ★ 布局算完再显示，避免先闪一下小窗口
        self.q = queue.Queue()
        self.busy = False
        self._sort_key, self._sort_desc = None, False
        self.var_filter = tk.StringVar(value="")
        self.var_count = tk.StringVar(value="")
        self._build()
        self.reload_table()
        self._fit_window()             # ★ 按内容定尺寸，然后锁死不给拉伸
        root.after(POLL_MS, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _fit_window(self):
        """开窗尺寸 = 理想尺寸，但必须容得下内容，且不超出屏幕；
        定好之后 resizable(False)：最大化按钮也会变灰。"""
        root = self.root
        root.update_idletasks()
        need_h = root.winfo_reqheight()          # 内容真实需要的高度（低于它就会裁切）
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()

        w = min(self.PREF_W, sw - 40)
        h = min(self.PREF_H, sh - 60)
        h = max(h, min(need_h + 16, sh - 50))    # 至少给内容留 16px 余量

        # 居中：y 里减掉标题栏高度，窗口框才真正居中而不是贴着屏幕顶
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h - 32) // 2)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.update_idletasks()
        root.resizable(False, False)             # ★ 宽度和高度都锁死
        root.deiconify()

    def _on_close(self):
        try:
            self.scan.cancel()
        except Exception:
            pass
        self.root.destroy()

    # ── 卡片容器 ───────────────────────────────────────
    @staticmethod
    def _card(parent, title=None, pad=(14, 12), top=12):
        """返回 (卡片, 内容区, 标题栏)。标题与内容共用同一张白卡。"""
        card = tk.Frame(parent, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        if title:
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x", padx=pad[0], pady=(top, 0))
            tk.Label(head, text=title, bg=CARD, fg=TEXT,
                     font=(FONT, 10, "bold")).pack(side="left")
            body = tk.Frame(card, bg=CARD)
            body.pack(fill="both", expand=True, padx=pad[0], pady=(10, pad[1]))
            return card, body, head
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="both", expand=True, padx=pad[0], pady=(top, pad[1]))
        return card, body, None

    def _build(self):
        self._init_style()

        # ══ 顶栏 ══
        head = tk.Frame(self.root, bg=CARD, height=64)
        head.pack(fill="x")
        head.pack_propagate(False)

        logo = tk.Canvas(head, width=34, height=34, bg=CARD, highlightthickness=0, bd=0)
        logo.pack(side="left", padx=(18, 10))
        round_rect(logo, 0, 0, 34, 34, 10, fill="#eef4ff", outline="")
        logo.create_text(17, 17, text="信", fill=PRIMARY, font=(FONT, 13, "bold"))

        titles = tk.Frame(head, bg=CARD)
        titles.pack(side="left", pady=(12, 0))
        tk.Label(titles, text="游戏信用分批量查询", bg=CARD, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(anchor="w")
        tk.Label(titles, text=f"腾讯游戏信用 · Q币资产一览 · v{APP_VERSION}",
                 bg=CARD, fg=MUTED, font=(FONT, 8)).pack(anchor="w")

        self.status_chip = StatusChip(head, "就绪", "ok")
        self.status_chip.pack(side="right", padx=(0, 20))

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        # ══ 底部日志（先占位，让中间区域自适应） ══
        logwrap = tk.Frame(self.root, bg=BG)
        logwrap.pack(side="bottom", fill="x", padx=16, pady=(0, 14))
        log_card, log_body, log_head = self._card(logwrap, "运行日志", pad=(14, 10), top=10)
        log_card.pack(fill="x")
        PillButton(log_head, "清空", command=self._clear_log, kind="plain",
                   height=24, width=52, radius=6, font=(FONT, 8)).pack(side="right")

        tk.Frame(log_body, bg=BORDER, height=1).pack(fill="x", pady=(2, 6))
        self.txt_log = tk.Text(log_body, height=4, state="disabled", wrap="word",
                               font=(FONT, 9), relief="flat", bd=0,
                               background=CARD, foreground=SUB,
                               highlightthickness=0, padx=0, pady=0, spacing1=1)
        sb = ttk.Scrollbar(log_body, orient="vertical", style="Modern.Vertical.TScrollbar",
                           command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", padx=(8, 0))
        self.txt_log.tag_configure("ts", foreground=MUTED)

        # ══ 中部：左表格 + 右操作栏 ══
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(14, 12))

        right = tk.Frame(body, bg=BG, width=self.RIGHT_W)
        right.pack(side="right", fill="y", padx=(14, 0))
        right.pack_propagate(False)

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True)

        self._build_table(left)
        self._build_side(right)

    # ── 表格 ────────────────────────────────────────────
    def _build_table(self, parent):
        card, body, head = self._card(parent, "账号列表", pad=(14, 12), top=12)
        card.pack(fill="both", expand=True)

        tk.Label(head, textvariable=self.var_count, bg=CARD, fg=MUTED,
                 font=(FONT, 8)).pack(side="left", padx=(8, 0))
        SearchBox(head, self.var_filter).pack(side="right")
        self.var_filter.trace_add("write", lambda *a: self.reload_table(log=False))

        tk.Frame(body, bg=BORDER, height=1).pack(fill="x")

        wrap = tk.Frame(body, bg=CARD)
        wrap.pack(fill="both", expand=True, pady=(2, 0))

        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLS],
                                 show="headings", selectmode="extended",
                                 style="Modern.Treeview")
        for key, title, w in self.COLS:
            anchor = "w" if key in ("nick", "qq") else "center"
            # ★ 表头锚点默认是 center，不会跟着列锚点走，必须显式给，否则
            #   昵称/QQ 的表头会跑到列中间，和左对齐的数据错位
            self.tree.heading(key, text=title, anchor=anchor,
                              command=lambda k=key: self.sort_by(k))
            # 昵称列吃掉多余宽度，窗口放大时不会在右侧留白
            self.tree.column(key, width=w, minwidth=w, anchor=anchor,
                             stretch=(key == "nick"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", style="Modern.Vertical.TScrollbar",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y", padx=(6, 0))

        for st, fg in STATE_FG.items():
            self.tree.tag_configure(st, foreground=fg)
        self.tree.tag_configure("odd", background="#ffffff")
        self.tree.tag_configure("even", background="#fafbfd")

        self._nick_font = tkfont.Font(family=FONT, size=10)
        self.nick_full = {}                 # iid -> 完整昵称，供悬停提示使用
        self._tip = None                    # 当前悬浮提示窗口
        self._tip_iid = None
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self._hide_tip())
        self.tree.bind("<MouseWheel>", lambda e: self._hide_tip(), add="+")
        self.tree.bind("<Button-1>", lambda e: self._hide_tip(), add="+")
        self.tree.bind("<Configure>", lambda e: self._hide_tip(), add="+")

        # ── 底部统计条：账号 / Q币 / 分数分布 + 占比条 ──
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(10, 0))
        foot = tk.Frame(body, bg=CARD)
        foot.pack(fill="x", pady=(9, 0))

        self.var_sum_qq = tk.StringVar(value="0")
        self.var_sum_qb = tk.StringVar(value="0.00")
        self.var_sum_high = tk.StringVar(value="0")
        self.var_sum_mid = tk.StringVar(value="0")
        self.var_sum_low = tk.StringVar(value="0")
        self.var_sum_black = tk.StringVar(value="0")
        self.var_sum_white = tk.StringVar(value="0")

        def metric(label, var, color, dot=None):
            box = tk.Frame(foot, bg=CARD)
            box.pack(side="left", padx=(0, 15))
            if dot:
                tk.Label(box, text="●", bg=CARD, fg=dot,
                         font=(FONT, 8)).pack(side="left", padx=(0, 4))
            tk.Label(box, text=label, bg=CARD, fg=MUTED,
                     font=(FONT, 8)).pack(side="left")
            tk.Label(box, textvariable=var, bg=CARD, fg=color,
                     font=(FONT, 11, "bold")).pack(side="left", padx=(4, 0))

        metric("账号", self.var_sum_qq, PRIMARY)
        metric("Q币", self.var_sum_qb, "#b45309")
        metric("350+", self.var_sum_high, OK_C, "#12b76a")
        metric("151-349", self.var_sum_mid, WARN_C, "#f79009")
        metric("≤150", self.var_sum_low, DANGER_C, "#f04438")
        metric("黑", self.var_sum_black, DANGER_C)
        metric("白", self.var_sum_white, OK_C)

        self.dist = DistBar(foot, height=6)
        self.dist.pack(side="right", fill="x", expand=True, padx=(18, 2), pady=4)

    # ── 右侧栏 ──────────────────────────────────────────
    def _build_side(self, parent):
        # ① 扫码授权
        card, body, _ = self._card(parent, "扫码授权", top=10)
        card.pack(fill="x")
        self.scan = ScanPanel(body, on_login=self._on_oauth_ok)
        self.scan.pack(fill="x")

        # ② 操作（吃掉右栏剩余高度，底边与左侧「账号列表」卡对齐）
        card, body, _ = self._card(parent, "操作", top=10, pad=(14, 10))
        card.pack(fill="both", expand=True, pady=(10, 0))
        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x")
        # 6 个等宽单元：第一行 3 个按钮，第二行 2 个各占一半
        for c in range(6):
            grid.columnconfigure(c, weight=1, uniform="btn")

        self.btn_score = PillButton(grid, "查分", kind="primary", height=30,
                                    command=lambda: self.refresh("all"))
        self.btn_score.grid(row=0, column=0, columnspan=2, sticky="ew",
                            padx=(0, 3), pady=(0, 6))
        self.btn_qb_all = PillButton(grid, "查币", kind="soft", height=30,
                                     command=lambda: self.refresh_qb("all"))
        self.btn_qb_all.grid(row=0, column=2, columnspan=2, sticky="ew",
                             padx=(3, 3), pady=(0, 6))
        self.btn_bw = PillButton(grid, "查黑白", kind="soft", height=30,
                                 command=self.refresh_bw)
        self.btn_bw.grid(row=0, column=4, columnspan=2, sticky="ew",
                         padx=(3, 0), pady=(0, 6))

        PillButton(grid, "导出 CSV", kind="ghost", height=28,
                   command=self.export_csv).grid(row=1, column=0, columnspan=3,
                                                 sticky="ew", padx=(0, 3), pady=(0, 6))
        PillButton(grid, "导入 CSV", kind="ghost", height=28,
                   command=self.import_csv).grid(row=1, column=3, columnspan=3,
                                                 sticky="ew", padx=(3, 0), pady=(0, 6))

        row = tk.Frame(grid, bg=CARD)
        row.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(0, 6))
        tk.Label(row, text="并发数", bg=CARD, fg=SUB, font=(FONT, 9)).pack(side="left")
        self.var_workers = tk.IntVar(value=3)
        Stepper(row, self.var_workers, 1, 5).pack(side="right")

        PillButton(grid, "删除选中账号", kind="danger", height=28,
                   command=self.del_selected).grid(row=3, column=0, columnspan=6,
                                                   sticky="ew")

        # 卡片底部被拉高后留白，用一句说明填住（贴底，不挤按钮）
        tk.Label(body, text="黑白依据活动接口应答判定，非官方评级",
                 bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w",
                 wraplength=self.RIGHT_W - 30).pack(side="bottom", fill="x",
                                                    pady=(8, 0))

    # ── ttk 主题 ────────────────────────────────────────
    def _init_style(self):
        sty = ttk.Style()
        try:
            sty.theme_use(THEME)
        except Exception:
            pass
        sty.configure(".", background=BG, foreground=TEXT, font=(FONT, 9),
                      borderwidth=0, focuscolor=BG)
        sty.configure("Modern.Treeview", background=CARD, fieldbackground=CARD,
                      foreground=TEXT, rowheight=32, borderwidth=0, relief="flat",
                      font=(FONT, 9))
        sty.map("Modern.Treeview",
                background=[("selected", "#e8f0fe")],
                foreground=[("selected", TEXT)])
        sty.layout("Modern.Treeview", [("Modern.Treeview.treearea", {"sticky": "nswe"})])
        sty.configure("Modern.Treeview.Heading", background="#ffffff", foreground=MUTED,
                      font=(FONT, 8, "bold"), relief="flat", borderwidth=0,
                      padding=(2, 8))
        sty.map("Modern.Treeview.Heading",
                background=[("active", "#f5f8fd")],
                foreground=[("active", PRIMARY)])
        sty.configure("Modern.Vertical.TScrollbar", gripcount=0, arrowsize=10,
                      background="#d5dbe5", darkcolor="#d5dbe5", lightcolor="#d5dbe5",
                      troughcolor=CARD, bordercolor=CARD, arrowcolor="#98a2b3",
                      relief="flat", width=8)
        sty.map("Modern.Vertical.TScrollbar",
                background=[("active", "#b8c2d0")])

    # ── 日志 ───────────────────────────────────────────
    def _log(self, msg):
        """往底部日志区追加一行带时间戳的日志。"""
        ts = time.strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"{ts}  ", "ts")
        self.txt_log.insert("end", f"{msg}\n")
        if int(self.txt_log.index("end-1c").split(".")[0]) > 300:
            self.txt_log.delete("1.0", "2.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ── 数据 ───────────────────────────────────────────
    def reload_table(self, log=True):
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        self.nick_full = {}
        accs = all_accounts()
        kw = self.var_filter.get().strip().lower()
        shown = [a for a in accs
                 if not kw or kw in (a["qq"] or "").lower()
                 or kw in (a["nick"] or "").lower()]
        for idx, a in enumerate(shown):
            qb = a.get("qb", 0)
            has_qb = bool(a.get("pay_token") or a.get("oa_token"))
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
            state = a["state"] if a["state"] in STATE_FG else "never"
            vals = [idx + 1, self._ellipsize(full_nick), a["qq"],
                    STATE_TXT.get(state, state), a.get("bw") or "-",
                    qb_prev_txt, qb_txt, qb_change,
                    score_prev_txt, score_txt, score_change]
            zebra = "even" if idx % 2 else "odd"
            iid = self.tree.insert("", "end", values=vals, tags=(state, zebra))
            self.rows[iid] = a["qq"]
            self.nick_full[iid] = full_nick
        self.var_count.set(f"{len(shown)} / {len(accs)} 个账号" if kw
                           else f"共 {len(accs)} 个账号")
        self._update_summary()
        if log:
            self._log(f"已加载 {len(accs)} 个账号")

    def _ellipsize(self, text):
        """按像素宽度截断文本并加省略号；放得下则原样返回。"""
        if not text or text == "-":
            return text
        fit = self.tree.column("nick", "width") - 10
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
        if not full or self._nick_font.measure(full) <= (self.tree.column("nick", "width") - 10):
            self._hide_tip()               # 完整显示时不弹提示
            return
        if self._tip_iid == iid and self._tip and self._tip.winfo_exists():
            return
        self._hide_tip()
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=full, background="#101828", fg="#ffffff",
                 relief="flat", borderwidth=0,
                 font=(FONT, 9), justify="left",
                 padx=8, pady=4).pack()
        x = self.tree.winfo_rootx() + event.x + 14
        y = self.tree.winfo_rooty() + event.y + 20
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
        """刷新右栏汇总：QQ数、Q币总额、350+ / 151-349 / ≤150。"""
        accs = all_accounts()
        qb_total = sum((a.get("qb", 0) or 0) for a in accs
                       if (a.get("oa_token")
                           or (a.get("pay_openid") and a.get("pay_token"))))
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
        bws = [a.get("bw") for a in accs if a.get("bw")]
        self.var_sum_black.set(str(sum(1 for v in bws if v == BLACK)))
        self.var_sum_white.set(str(sum(1 for v in bws if v == WHITE)))
        self.dist.set(high, mid, low)

    def sort_by(self, key):
        """点表头排序：同一列再点一次切换升/降序，并在表头显示箭头。"""
        if key == self._sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = key, False
        items = [(self.tree.set(i, key), i) for i in self.tree.get_children()]

        def num(txt):
            try:
                return float(str(txt).replace("+", ""))
            except ValueError:
                return 0.0

        try:
            items.sort(key=lambda x: num(x[0]))
        except Exception:
            items.sort(key=lambda x: x[0])
        if self._sort_desc:
            items.reverse()
        for pos, (_, iid) in enumerate(items):
            self.tree.move(iid, "", pos)
        for k, title, _w in self.COLS:
            mark = ("  ↑" if not self._sort_desc else "  ↓") if k == key else ""
            self.tree.heading(k, text=title + mark)

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
        elif k == "bw_row":
            self._paint_bw_row(ev[1], ev[2])
        elif k == "finish_bw":
            stat = ev[1]
            self.busy = False
            self._set_buttons(True)
            self._update_summary()
            self.status_chip.set("就绪", "ok")
            self._log(f"黑白检测完成 · 白 {stat.get(WHITE, 0)} / 黑 {stat.get(BLACK, 0)}"
                      f" / 失效 {stat.get(DEAD, 0)} / 未知 {stat.get('未知', 0)}")
        elif k == "prog":
            pass   # 进度信息由日志承担
        elif k == "finish":
            ok, fail, exp = ev[1], ev[2], ev[3]
            self.busy = False
            self._set_buttons(True)
            self._update_summary()
            self.status_chip.set("就绪", "ok")
            self._log(f"信用分刷新完成 · 成功 {ok} / 失败 {fail} / 凭据过期 {exp}")
        elif k == "finish_qb":
            ok, fail = ev[1], ev[2]
            self.busy = False
            self._set_buttons(True)
            self._update_summary()
            self.status_chip.set("就绪", "ok")
            self._log(f"Q币刷新完成 · 成功 {ok} / 失败 {fail}")
        elif k == "login_ok":
            self.reload_table(log=False)
            self._log(f"已添加账号 {ev[1]}")
        elif k == "log":
            self._log(ev[1])          # ★ 子线程只投递文本，落地由 UI 线程做
        elif k == "finish_login":
            self.busy = False
            self._set_buttons(True)
            self._update_summary()
            self.status_chip.set("就绪", "ok")
            self._log(f"单码查询完成 QQ {ev[1]}（信用分 / Q币 / 黑白）")

    def _paint_row(self, qq, res, err):
        iid = next((i for i, v in self.rows.items() if v == qq), None)
        if not iid:
            self.reload_table(log=False); return
        if res and res.ok:
            st = "ok"
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
        self.tree.set(iid, "state", STATE_TXT[st])
        zebra = "even" if "even" in self.tree.item(iid, "tags") else "odd"
        self.tree.item(iid, tags=(st, zebra))

    def _paint_qb_row(self, qq, bal, err):
        iid = next((i for i, v in self.rows.items() if v == qq), None)
        if not iid:
            self.reload_table(log=False); return
        if bal is not None:
            acc = next((a for a in all_accounts() if a["qq"] == qq), None)
            self.tree.set(iid, "qb", self._format_qb(bal))
            self.tree.set(iid, "qb_prev",
                          self._format_qb(acc.get("qb_prev")) if acc else "-")
            self.tree.set(iid, "qb_change",
                          self._format_change(bal, acc.get("qb_prev"), 100)
                          if acc else "-")

    def _paint_bw_row(self, qq, verdict):
        iid = next((i for i, v in self.rows.items() if v == qq), None)
        if not iid:
            self.reload_table(log=False); return
        self.tree.set(iid, "bw", verdict)

    def _set_buttons(self, on):
        st = "normal" if on else "disabled"
        for b in (self.btn_score, self.btn_qb_all, self.btn_bw):
            b.config(state=st)

    # ── 动作 ───────────────────────────────────────────
    def _on_oauth_ok(self, res):
        """单码登录成功：存凭据 → 刷新表格 → 后台把三条查询一次跑完。"""
        ensure_account(res.qq, res.nick)
        update_oauth(res.qq, res.openid, res.access_token, res.appid, res.exp_ts)
        self.reload_table(log=False)
        self._log(f"单码登录成功 QQ {res.qq} {res.nick!r} · 凭据 {days_left(res.exp_ts)} 天")
        messagebox.showinfo(
            "登录成功",
            f"QQ {res.qq}\n昵称 {res.nick}\n"
            f"已同时授权：信用分 / Q币 / 黑白\n"
            f"（凭据约 {days_left(res.exp_ts)} 天有效，即将自动查询）",
            parent=self)
        self.status_chip.set("查询中…", "busy")
        self.busy = True
        self._set_buttons(False)
        threading.Thread(target=self._query_all_after_login, args=(res,),
                         daemon=True).start()

    def _query_all_after_login(self, res):
        """扫码后立即跑：信用分 → Q币 → 黑白，全都用这一份 token。

        ★ 这里在子线程里，绝不能碰任何 Tk 控件（会 RuntimeException:
          main thread is not in main loop），日志也走事件队列。
        """
        qq = res.qq
        try:
            r = query_credit_token(res.openid, res.access_token, res.appid)
            if r.ok:
                update_result(qq, r, "ok")
                self.q.put(("row", qq, r, ""))
                self.q.put(("log", f"信用分 {r.score}"))
            else:
                state = "expired" if r.expired else "error"
                update_result(qq, _EMPTY, state, r.reason)
                self.q.put(("row", qq, r, r.reason))
                self.q.put(("log", f"信用分失败：{r.reason}"))
        except Exception as e:
            self.q.put(("log", f"信用分异常：{type(e).__name__}: {e}"))

        try:
            from pay import query_qb
            bal = query_qb(res.openid, res.access_token)
            update_qb_value(qq, bal["qb_balance"])
            self.q.put(("qb_row", qq, bal["qb_balance"], ""))
            self.q.put(("log", f"Q币 {bal['q币']:.2f}"))
        except Exception as e:
            self.q.put(("log", f"Q币失败：{type(e).__name__}: {e}"))

        try:
            b = bw_check(res.openid, res.access_token, res.appid)
            update_bw(qq, b.verdict, b.msg or b.reason, b.ret)
            self.q.put(("bw_row", qq, b.verdict))
            self.q.put(("log", f"黑白 {b.verdict}（{(b.msg or '')[:24]}）"))
        except Exception as e:
            self.q.put(("log", f"黑白失败：{type(e).__name__}: {e}"))

        self.q.put(("finish_login", qq))

    def refresh(self, mode):
        if self.busy:
            messagebox.showinfo("提示", "已有任务在运行"); return
        accs = all_accounts()
        # 既没有 gs_code 也没有单码 token 的账号查不了分，先剔掉免得白发请求
        accs = [a for a in accs if a.get("gs_code") or a.get("oa_token")]
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
            accs = [a for a in accs if a["query_at"] < t0]
            if not accs:
                messagebox.showinfo("提示", "今天所有账号都已查询过"); return
        else:
            # 全部：仍先按 exp 剔除已过期，一个包都不发
            now = time.time()
            def _exp(a):
                return jwt_exp(a.get("gs_code", "")) or int(a.get("oa_exp", 0) or 0)
            dead = [a["qq"] for a in accs if _exp(a) and _exp(a) <= now]
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
        self.status_chip.set("查询中…", "busy")
        n = max(1, min(5, int(self.var_workers.get() or 3)))
        self._log(f"开始刷新信用分 {len(accs)} 个账号（并发 {n}）")
        threading.Thread(target=self._run_refresh, args=(accs, n), daemon=True).start()

    @staticmethod
    def _credit_query(acc):
        """单码账号走 token，老账号走 gs_id/gs_code。"""
        if acc.get("oa_token"):
            return query_credit_token(acc["oa_openid"], acc["oa_token"],
                                      acc.get("oa_appid") or "101477677")
        return query_credit(acc["gs_id"], acc["gs_code"])

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
            Scheduler(workers=n, query=self._credit_query).refresh(
                accs, on_res, lambda d, t: self.q.put(("prog", d, t)))
        finally:
            self.q.put(("finish", stat["ok"], stat["fail"], stat["exp"]))

    # ── Q币刷新 ───────────────────────────────────────
    def refresh_qb(self, mode):
        if self.busy:
            messagebox.showinfo("提示", "已有任务在运行"); return
        accs = all_accounts()
        # 单码 token 或 pay 侧 token 都能查币
        accs = [a for a in accs
                if a.get("oa_token") or (a.get("pay_openid") and a.get("pay_token"))]
        if not accs:
            messagebox.showinfo("提示", "暂无可用凭据（先扫码登录，或做一次支付授权）"); return

        if mode == "selected":
            sel = {self.rows[i] for i in self.tree.selection() if i in self.rows}
            accs = [a for a in accs if a["qq"] in sel]
            if not accs:
                messagebox.showinfo("提示", "请先在列表中选择已授权的账号"); return

        self.busy = True
        self._set_buttons(False)
        self.status_chip.set("查询中…", "busy")
        n = max(1, min(5, int(self.var_workers.get() or 3)))
        self._log(f"开始刷新 Q币 {len(accs)} 个账号（并发 {n}）")
        threading.Thread(target=self._run_refresh_qb, args=(accs, n), daemon=True).start()

    @staticmethod
    def _qb_cred(acc):
        """返回 (openid, token, 是否单码)。单码优先，其次 pay 侧。"""
        if acc.get("oa_token"):
            return acc["oa_openid"], acc["oa_token"], True
        return acc.get("pay_openid", ""), acc.get("pay_token", ""), False

    def _run_refresh_qb(self, accs, n):
        from pay import refresh_qb as _refresh_qb, PayError
        from store import update_pay
        stat = {"ok": 0, "fail": 0}
        total = len(accs)
        for i, a in enumerate(accs):
            openid, token, is_oa = self._qb_cred(a)
            try:
                bal = _refresh_qb(openid, token)
                if is_oa:
                    # 单码 token 属于 101477677，不能写进 pay_*
                    update_qb_value(a["qq"], bal["qb_balance"])
                else:
                    update_pay(a["qq"], openid, token,
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

    # ── 黑白检测（单码 token 优先，其次 pay 侧）───────────
    def refresh_bw(self):
        """判定规则：活动接口 msg == 仓库券礼包领取参数不满足 → 白；
                       msg == 活动太火爆 → 失效；其它（账号存在异常）→ 黑。"""
        if self.busy:
            messagebox.showinfo("提示", "已有任务在运行"); return
        accs = [a for a in all_accounts()
                if a.get("oa_token") or (a.get("pay_openid") and a.get("pay_token"))]
        if not accs:
            messagebox.showinfo(
                "提示",
                "暂无可用凭据。\n黑白检测需要 openid/access_token，请先扫码登录。")
            return

        self.busy = True
        self._set_buttons(False)
        self.status_chip.set("查黑白中…", "busy")
        n = max(1, min(5, int(self.var_workers.get() or 3)))
        self._log(f"开始检测黑白 {len(accs)} 个账号（并发 {n}）")
        threading.Thread(target=self._run_bw, args=(accs, n), daemon=True).start()

    def _run_bw(self, accs, n):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from pay import PAY_CLIENT_ID
        stat = {WHITE: 0, BLACK: 0, DEAD: 0, "未知": 0}

        def one(a):
            if a.get("oa_token"):
                r = bw_check(a["oa_openid"], a["oa_token"],
                             a.get("oa_appid") or "101477677")
            else:
                # ★ appid 必须与 token 签发方一致，pay 侧就是 101502376
                r = bw_check(a["pay_openid"], a["pay_token"], PAY_CLIENT_ID)
            update_bw(a["qq"], r.verdict, r.msg or r.reason, r.ret)
            return a, r

        try:
            with ThreadPoolExecutor(max_workers=n) as ex:
                futs = [ex.submit(one, a) for a in accs]
                for f in as_completed(futs):
                    try:
                        a, r = f.result()
                    except Exception as e:
                        stat["未知"] += 1
                        self.q.put(("bw_row", "", "未知"))
                        self.q.put(("log", f"黑白检测异常：{type(e).__name__}: {e}"))
                        continue
                    stat[r.verdict] = stat.get(r.verdict, 0) + 1
                    self.q.put(("bw_row", a["qq"], r.verdict))
        finally:
            self.q.put(("finish_bw", stat))

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
                        "状态", "黑白", "分数更新时间", "最近查询", "备注"])
            for a in all_accounts():
                exp = jwt_exp(a["gs_code"])
                qb = a.get("qb", 0)
                qb_txt = (f"{qb / 100.0:.2f}"
                          if ((a.get("oa_token") or a.get("pay_token")) and qb) else "")
                w.writerow([a["qq"], a["nick"], a["score"], qb_txt, days_left(exp),
                            time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "",
                            STATE_TXT.get(a["state"], a["state"]),
                            a.get("bw", ""),
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
        ttk.Style().theme_use(THEME)
    except Exception:
        pass
    App(root)
    root.mainloop()
