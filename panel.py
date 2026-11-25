#!/usr/bin/env python3
"""PRL 控制台 —— 横版 16:9 桌面面板：状态 / 算力 / 过去24小时 / 电脑情况 / 开停
数据来源：本机控制口 127.0.0.1:18889、看板 API、nvidia-smi
"""
import ctypes
import json
import os
import ssl
import subprocess
import threading
import time
import tkinter as tk
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CTL = "http://127.0.0.1:18889/ctl"
def _cfg():
    try:
        with open(os.path.join(BASE, "agent", "config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_CFG = _cfg()
WORKER = _CFG.get("worker") or "rig-a"
DASH = (os.environ.get("PRL_DASHBOARD") or _CFG.get("dashboard_url")
        or "http://127.0.0.1:8088").rstrip("/")
RIG = DASH + "/api/rig"
EARN = DASH + "/api/earnings"
PANEL = DASH + "/"
GUARDIAN = os.path.join(BASE, "guardian.py")
NO_WINDOW = 0x08000000

_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)

BG = "#f6f6f3"
CARD = "#ffffff"
LINE = "#e6e4dd"
TXT = "#16160f"
MUT = "#7a7a70"
GRN = "#0f7a4a"
RED = "#d92d20"
AMB = "#9a5b00"

F = "Microsoft YaHei UI"      # 字体族
W_WIN, H_WIN = 900, 500       # 16:9


def get_json(url, timeout=6):
    with _OPENER.open(urllib.request.Request(url, headers={"User-Agent": "prl-panel/2.0"}),
                      timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ctl(action=None):
    url = CTL if not action else f"{CTL}?action={action}"
    return get_json(url, timeout=6)


def gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,temperature.gpu,fan.speed,power.draw,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6, creationflags=NO_WINDOW).stdout.strip()
        p = [x.strip() for x in out.split(",")]
        return {"name": p[0], "temp": float(p[1]), "fan": float(p[2]),
                "power": float(p[3]), "util": float(p[4])}
    except Exception:
        return None


class FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class SysStat:
    def __init__(self):
        self.prev = None

    @staticmethod
    def _ft(ft):
        return (ft.high << 32) | ft.low

    def cpu(self):
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        cur = (self._ft(idle), self._ft(kern), self._ft(user))
        pct = 0.0
        if self.prev:
            di = cur[0] - self.prev[0]
            dt = (cur[1] - self.prev[1]) + (cur[2] - self.prev[2])
            if dt > 0:
                pct = max(0.0, min(100.0, (dt - di) / dt * 100.0))
        self.prev = cur
        return pct

    @staticmethod
    def mem():
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.dwMemoryLoad, m.ullTotalPhys / 2**30, m.ullAvailPhys / 2**30


class Panel:
    def __init__(self, root):
        self.root = root
        self.sys = SysStat()
        self.last = {}
        self._icon = None

        root.title("PRL 控制台")
        root.configure(bg=BG)
        root.geometry(f"{W_WIN}x{H_WIN}")
        root.minsize(720, 405)
        root.resizable(False, False)
        try:
            from PIL import Image, ImageTk
            self._icon = ImageTk.PhotoImage(Image.open(os.path.join(BASE, "prl_icon_64.png")))
            root.iconphoto(True, self._icon)
        except Exception:
            pass
        try:
            root.iconbitmap(os.path.join(BASE, "prl.ico"))
        except Exception:
            pass

        # ---------- 顶栏 ----------
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=22, pady=(16, 8))
        tk.Label(top, text="PRL 控制台", bg=BG, fg=TXT, font=(F, 18, "bold")).pack(side="left")
        self.topmost = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="置顶", variable=self.topmost, bg=BG, fg=MUT,
                       activebackground=BG, selectcolor=CARD, font=(F, 10, "bold"),
                       command=self.toggle_top).pack(side="right", padx=(10, 0))
        self.status = tk.Label(top, text="● 读取中…", bg=BG, fg=MUT, font=(F, 14, "bold"))
        self.status.pack(side="left", padx=(18, 0))
        self.hint = tk.Label(top, text="", bg=BG, fg=MUT, font=(F, 9, "bold"))
        self.hint.pack(side="left", padx=(10, 0))

        # ---------- 五个大数值卡 ----------
        cards = tk.Frame(root, bg=BG)
        cards.pack(fill="x", padx=18, pady=(2, 6))
        self.c_th, self.u_th = self._card(cards, 0, "算力", "TH/s")
        self.c_pw, self.u_pw = self._card(cards, 1, "功耗", "W")
        self.c_tc, self.u_tc = self._card(cards, 2, "温度", "°C", value_fg=RED)
        self.c_24, self.u_24 = self._card(cards, 3, "过去 24 小时", "PRL", value_fg=RED)
        self.c_net, self.u_net = self._card(cards, 4, "日净利（预估）", "元/天", value_fg=GRN)
        for i in range(5):
            cards.grid_columnconfigure(i, weight=1, uniform="c")

        self.note = tk.Label(root,
                             text="日净利为预估值，与实际运行时间、功率稳定性有关（打游戏 / 高温保护 / 断线时会低于预估）",
                             bg=BG, fg=MUT, font=(F, 9, "bold"))
        self.note.pack(anchor="w", padx=18, pady=(0, 6))

        # ---------- 电脑信息条 ----------
        info = tk.Frame(root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        info.pack(fill="x", padx=18, pady=(0, 10))
        self.i_cpu = self._info_cell(info, 0, "CPU / 内存")
        self.i_gpu = self._info_cell(info, 1, "显卡")
        self.i_thm = self._info_cell(info, 2, "温度保护")
        for i in range(3):
            info.grid_columnconfigure(i, weight=1, uniform="i")

        # ---------- 按钮 ----------
        btns = tk.Frame(root, bg=BG)
        btns.pack(fill="x", padx=18, pady=(0, 16))
        self.btn_start = tk.Button(btns, text="▶  开始挖矿", command=lambda: self.act("start"),
                                   bg="#e7f6ee", fg=GRN, relief="flat",
                                   font=(F, 12, "bold"), activebackground="#d5efe1", cursor="hand2")
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 6), ipady=7)
        self.btn_stop = tk.Button(btns, text="■  停止挖矿", command=lambda: self.act("stop"),
                                  bg="#fdecea", fg=RED, relief="flat",
                                  font=(F, 12, "bold"), activebackground="#fbd9d6", cursor="hand2")
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(6, 0), ipady=7)

        sheet = tk.Frame(root, bg=BG)
        sheet.pack(fill="x", padx=18, pady=(0, 16))
        tk.Button(sheet, text="打开网页看板", command=self.open_panel, bg=CARD, fg=TXT,
                  relief="flat", highlightbackground=LINE, highlightthickness=1,
                  font=(F, 10, "bold"), cursor="hand2").pack(side="left", expand=True, fill="x",
                                                            padx=(0, 5), ipady=5)
        tk.Button(sheet, text="刷新", command=self.refresh, bg=CARD, fg=TXT,
                  relief="flat", highlightbackground=LINE, highlightthickness=1,
                  font=(F, 10, "bold"), cursor="hand2").pack(side="left", expand=True, fill="x",
                                                            padx=(5, 0), ipady=5)

        try:
            root.update_idletasks()
            root.geometry(f"{W_WIN}x{H_WIN}")
        except Exception:
            pass

        self.refresh()
        self.root.after(3000, self.loop)

    # ---------- 组件 ----------
    def _card(self, parent, col, title, unit, value_fg=TXT):
        f = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        f.grid(row=0, column=col, sticky="nsew", padx=6)
        tk.Label(f, text=title, bg=CARD, fg=MUT, font=(F, 11, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
        val = tk.Label(f, text="—", bg=CARD, fg=value_fg, font=(F, 26, "bold"))
        val.pack(anchor="w", padx=12, pady=(1, 0))
        ul = tk.Label(f, text=unit, bg=CARD, fg=MUT, font=(F, 9, "bold"))
        ul.pack(anchor="w", padx=12, pady=(0, 10))
        return val, ul

    def _info_cell(self, parent, col, title):
        f = tk.Frame(parent, bg=CARD)
        f.grid(row=0, column=col, sticky="nsew", padx=8, pady=7)
        tk.Label(f, text=title, bg=CARD, fg=MUT, font=(F, 9, "bold")).pack(anchor="w")
        val = tk.Label(f, text="—", bg=CARD, fg=TXT, font=(F, 11, "bold"))
        val.pack(anchor="w")
        return val

    def _style_btn(self, btn, active, text, color):
        """active=True：实色高亮可点；False：灰底不可点，文字直接说明状态。"""
        if active:
            btn.config(text=text, state="normal", bg=color, fg="#ffffff",
                       activebackground=color, activeforeground="#ffffff",
                       disabledforeground="#ffffff", relief="flat")
        else:
            btn.config(text=text, state="disabled", bg="#eceae4", fg="#a9a79f",
                       disabledforeground="#a9a79f", activebackground="#eceae4",
                       relief="flat")

    def update_buttons(self, kind):
        """kind: running / auto / stopped / down"""
        if kind == "running":
            self._style_btn(self.btn_start, False, "✔  正在挖矿", GRN)
            self._style_btn(self.btn_stop, True, "■  停止挖矿", RED)
        elif kind == "auto":
            self._style_btn(self.btn_start, False, "⏸  自动暂停中", GRN)
            self._style_btn(self.btn_stop, True, "■  停止挖矿", RED)
        elif kind == "down":
            self._style_btn(self.btn_start, True, "▶  开始挖矿", GRN)
            self._style_btn(self.btn_stop, False, "已停止（守护未运行）", RED)
        else:
            self._style_btn(self.btn_start, True, "▶  开始挖矿", GRN)
            self._style_btn(self.btn_stop, False, "⏹  已停止", RED)

    def toggle_top(self):
        self.root.attributes("-topmost", self.topmost.get())

    # ---------- 动作 ----------
    def act(self, action):
        try:
            ctl(action)
            self.hint.config(text="已发送：" + ("开始" if action == "start" else "停止") + "，约 5 秒生效")
        except Exception:
            if action == "start":
                self.spawn_guardian()
                self.hint.config(text="守护未运行，已尝试启动")
            else:
                self.hint.config(text="控制口连不上（守护未运行）")
        self.root.after(1500, self.refresh)

    def spawn_guardian(self):
        exe = sys_exec()
        subprocess.Popen([exe, GUARDIAN], cwd=BASE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | NO_WINDOW)

    def open_panel(self):
        os.startfile(PANEL)

    def refresh(self):
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        data = {}
        try:
            data["ctl"] = ctl()
        except Exception:
            data["ctl"] = None
        try:
            data["rig"] = get_json(f"{RIG}?w={WORKER}")
        except Exception:
            data["rig"] = None
        try:
            data["earn"] = get_json(f"{EARN}?w={WORKER}")
        except Exception:
            data["earn"] = None
        data["gpu"] = gpu()
        data["cpu"] = self.sys.cpu()
        data["mem"] = self.sys.mem()
        self.last = data
        self.root.after(0, lambda: self.render(data))

    # ---------- 渲染 ----------
    def render(self, d):
        c = d.get("ctl")
        rig = d.get("rig") or {}
        earn = d.get("earn") or {}
        st = c or {}
        state = (st.get("state") or "")

        if not c:
            self.status.config(text="● 守护未运行", fg=RED)
            self.hint.config(text="点「开始挖矿」")
            running = False
        elif st.get("manual"):
            self.status.config(text="● 已手动暂停", fg=AMB)
            self.hint.config(text="点「开始挖矿」恢复")
            running = False
        elif "thermal" in state:
            self.status.config(text="● 高温保护中", fg=AMB)
            self.hint.config(text="过热已自动停矿，降温后自动恢复")
            running = False
        elif st.get("game"):
            self.status.config(text="● 游戏暂停中", fg=AMB)
            self.hint.config(text=f"检测到 {st['game']}，退出 10 秒后恢复")
            running = False
        else:
            self.status.config(text="● 挖矿中", fg=GRN)
            self.hint.config(text="")
            running = state == "mining"

        if not c:
            kind = "down"
        elif st.get("manual"):
            kind = "stopped"
        elif "thermal" in state or st.get("game"):
            kind = "auto"
        elif running:
            kind = "running"
        else:
            kind = "stopped"
        self.update_buttons(kind)

        h24 = earn.get("last24_prl") or 0
        px = earn.get("price_usd") or 0

        if running:
            self.c_th.config(text=f"{rig.get('total_th') or 0:.1f}")
            self.c_pw.config(text=f"{rig.get('power_w') or 0:.0f}")
            self.c_tc.config(text=f"{rig.get('max_temp') or 0:.0f}")
            prof = rig.get("profit_cny_day")
            self.c_net.config(text=f"¥{prof:.0f}" if prof is not None else "—")
        else:
            for w in (self.c_th, self.c_pw, self.c_tc, self.c_net):
                w.config(text="—")
        self.c_24.config(text=f"{h24:.3f}")
        self.u_24.config(text=f"PRL ≈¥{h24 * px * 7.2:.2f}")

        load, total, avail = d["mem"]
        self.i_cpu.config(text=f"{d['cpu']:.0f}%   {total - avail:.1f}/{total:.0f} G   ({load}%)")

        g = d.get("gpu")
        if g:
            nm = (g.get("name") or "").replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
            hot = g["temp"] >= 75
            self.i_gpu.config(text=f"{nm} · {g['temp']:.0f}°C · {g['power']:.0f}W · {g['fan']:.0f}%",
                              fg=RED if hot else TXT)
            if hot and running:
                self.hint.config(text=f"温度 {g['temp']:.0f}°C 偏高（≥80°C 自动停矿）")
        else:
            self.i_gpu.config(text="—")

        peak = st.get("peak")
        hot_t = st.get("hot") or 80
        cool_t = st.get("cool") or 70
        self.i_thm.config(text=(f"峰值 {peak:.0f}°C · {hot_t}/{cool_t}°C"
                                if peak else f"停 {hot_t}°C · 恢复 {cool_t}°C"))

    def loop(self):
        self.refresh()
        self.root.after(3000, self.loop)


def sys_exec():
    import sys as _s
    exe = getattr(_s, "executable", "") or ""
    if exe.lower().endswith("pythonw.exe"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


if __name__ == "__main__":
    root = tk.Tk()
    Panel(root)
    root.mainloop()
