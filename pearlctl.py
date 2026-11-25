#!/usr/bin/env python3
"""PRL 控制台桌面控制器：start / stop / status
- start  : 已在跑就提示，否则后台拉起 guardian
- stop   : 结束 guardian 及其子进程（矿工/隧道/上报端）
- status : 弹窗显示当前状态 + 实时算力/收益（点「打开看板」直接跳网页）
"""
import ctypes
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "guardian.state.json")
GUARDIAN = os.path.join(BASE, "guardian.py")
def _cfg():
    try:
        import json as _j
        with open(os.path.join(BASE, "agent", "config.json"), "r", encoding="utf-8") as f:
            return _j.load(f)
    except Exception:
        return {}


_CFG = _cfg()
DASH = (os.environ.get("PRL_DASHBOARD") or _CFG.get("dashboard_url")
        or "http://127.0.0.1:8088").rstrip("/")
API = DASH + "/api/rig"
PANEL = DASH + "/"

MB_OK, MB_YESNO, MB_ICONINFO, MB_ICONWARN, MB_ICONERROR = 0x0, 0x4, 0x40, 0x30, 0x10
IDYES = 6
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


QUIET = "--quiet" in sys.argv or os.environ.get("PRL_CTL_QUIET") == "1"


def msg(text, title="PRL 控制台", flags=MB_OK | MB_ICONINFO):
    if QUIET:
        print(f"[{title}]\n{text}\n")
        return IDYES if ("--yes" in sys.argv) else 2
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags)


def read_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def guardian_alive(st):
    if not st:
        return False
    pid, ts = st.get("pid"), st.get("ts", 0)
    if not pid or time.time() - ts > 30:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout
        return str(pid) in out
    except Exception:
        return False


def api():
    try:
        with _OPENER.open(urllib.request.Request(API + "?t=" + str(int(time.time())),
                                                 headers={"User-Agent": "prl-ctl/1.0"}), timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def fmt(v, d=2):
    return "—" if v is None else f"{v:.{d}f}"


def py_for_child():
    """用 python.exe 跑守护（pythonw 下没有 stdout，日志会丢）。"""
    exe = sys.executable or ""
    if exe.lower().endswith("pythonw.exe"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


def do_start():
    st = read_state()
    if guardian_alive(st):
        msg("守护进程已经在跑了。\n\n当前状态：" + describe(st))
        return
    py = py_for_child()
    subprocess.Popen([py, GUARDIAN], cwd=BASE,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x08000000)  # DETACHED | NO_WINDOW
    time.sleep(2)
    st = None
    for _ in range(10):
        st = read_state()
        if guardian_alive(st):
            break
        time.sleep(1)
    if guardian_alive(st):
        msg("已启动。\n\n" + describe(st) + "\n\n打游戏会自动暂停，退出后自动恢复。")
    else:
        msg("启动失败，请查看 guardian.log", flags=MB_OK | MB_ICONERROR)


def do_stop():
    st = read_state()
    if not guardian_alive(st):
        msg("守护进程本来就没在跑。")
        return
    pid = st["pid"]
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
    subprocess.run(["taskkill", "/F", "/IM", "miner.exe"], capture_output=True, creationflags=NO_WINDOW)
    subprocess.run(["taskkill", "/F", "/IM", "onezerominer.exe"], capture_output=True, creationflags=NO_WINDOW)
    time.sleep(2)
    msg("已停止。\n\nGPU 已完全释放，隧道和上报端也一起关了。\n要恢复挖矿，双击「PRL 控制台 开」。")


def describe(st):
    if not st:
        return "未运行"
    s = st.get("state", "?")
    g = st.get("game")
    if s.startswith("mining"):
        return "挖矿中"
    if s.startswith("paused"):
        if "manual" in s:
            return "已手动暂停"
        return f"已暂停（{g} 正在运行）" if g else "已暂停"
    if s.startswith("resume in"):
        return "即将恢复挖矿"
    return s


def do_status():
    st = read_state()
    alive = guardian_alive(st)
    d = api()
    lines = []
    lines.append("状态：" + (describe(st) if alive else "未运行（双击「PRL 控制台 开」启动）"))
    if d:
        lines.append("")
        lines.append(f"算力：{fmt(d.get('total_th'))} TH/s")
        lines.append(f"功耗：{fmt(d.get('power_w'), 0)} W   温度：{fmt(d.get('max_temp'), 0)} °C")
        lines.append(f"日产出：{fmt(d.get('est_day_prl'), 3)} PRL")
        profit = d.get("profit_cny_day")
        lines.append(f"日净利：{'¥' + fmt(profit) if profit is not None else '—'}")
        if d.get("online") and d.get("uptime_s"):
            h = d["uptime_s"] // 3600
            m = d["uptime_s"] % 3600 // 60
            lines.append(f"本次运行：{h} 小时 {m} 分")
        lines.append("")
        lines.append(f"PRL ${fmt(d.get('price_usd'), 3)}   全网 {fmt(d.get('net_eh'), 1)} EH/s")
    else:
        lines.append("")
        lines.append("（取不到看板数据，检查网络）")
    lines.append("")
    lines.append("点「是」打开网页看板，点「否」关闭。")
    if msg("\n".join(lines), "PRL 控制台 · 状态", MB_YESNO | MB_ICONINFO) == IDYES:
        os.startfile(PANEL)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"start": do_start, "stop": do_stop, "status": do_status}.get(cmd, do_status)()
