#!/usr/bin/env python3
"""PRL 矿机上报端：读 OneZeroMiner 本地 API + nvidia-smi，每 N 秒 POST 到看板。
全网算力/价格/收益换算都在 VPS 侧完成，本机只负责报「算力 + 硬件状态」。

用法：
  python rig_agent.py            # 常驻
  python rig_agent.py --once     # 跑一次退出（排错）
  python rig_agent.py --debug    # 打印矿工 API 原始 JSON 后退出
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)                    # ...\pearl
CFG_PATH = os.path.join(HERE, "config.json")
MINER_API = "http://127.0.0.1:3001/"
MANUAL = os.path.join(BASE, "manual.pause")      # 存在 = 手动暂停
STATE_FILE = os.path.join(BASE, "guardian.state.json")
GUARDIAN = os.path.join(BASE, "guardian.py")
CTL_PORT = 18889                                 # 本机网页控制端口
CMD_SEQ_FILE = os.path.join(BASE, "cmd.seq")       # 已执行到第几条远程命令
_LAST_SEQ = None
NO_WINDOW = 0x08000000

# 显式忽略系统代理，直连看板
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)

_online_since = None


def load_cfg():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def http_json(url, timeout=8, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def gpu_stats():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,temperature.gpu,fan.speed,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000 if os.name == "nt" else 0,   # CREATE_NO_WINDOW
        ).stdout.strip().splitlines()
    except Exception:
        return []
    res = []
    for line in out:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue

        def f(x):
            try:
                return float(x) if x not in ("N/A", "", "[N/A]") else None
            except ValueError:
                return None

        res.append((parts[0], f(parts[1]), f(parts[2]), f(parts[3])))
    return res


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def miner_raw():
    try:
        return http_json(MINER_API, timeout=5)
    except Exception:
        return None


def miner_normalized(raw):
    if not raw:
        return None, []
    devs = raw.get("devices") or raw.get("gpus") or raw.get("devs") or []
    if isinstance(devs, dict):
        devs = list(devs.values())
    out = []
    for d in devs if isinstance(devs, list) else []:
        if not isinstance(d, dict):
            continue
        name = d.get("name") or d.get("model") or d.get("gpu") or "GPU"
        hs = None
        for k in ("hashrate", "hash_rate", "hr", "speed", "total_hashrate"):
            if k in d and num(d[k]) is not None:
                hs = num(d[k])
                break
        if hs is None:
            for k, v in d.items():
                if "hash" in k.lower() and isinstance(v, (int, float)):
                    hs = float(v)
                    break
        acc = rej = None
        for k in ("accepted", "shares_accepted", "acc", "accepted_shares", "valid"):
            if k in d:
                acc = d[k]
                break
        for k in ("rejected", "shares_rejected", "rej", "rejected_shares", "invalid"):
            if k in d:
                rej = d[k]
                break
        out.append({"name": name, "hs": hs, "acc": acc, "rej": rej})

    total = None
    t = raw.get("total")
    if isinstance(t, dict):
        for k in ("hashrate", "hash_rate", "speed", "total_hashrate"):
            if k in t and num(t[k]) is not None:
                total = num(t[k])
                break
    if total is None:
        total = num(raw.get("hashrate") or raw.get("hash_rate"))
    if total is None:
        vals = [d["hs"] for d in out if d["hs"] is not None]
        total = sum(vals) if vals else None
    return total, out


def to_th(hs):
    if hs is None:
        return None
    return hs / 1e12 if hs > 1e9 else hs


def official_log_stats(path):
    """官方 pfminer 没有本地 API，从日志里读 proof_per_sec 和 share 计数。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(8192).decode("utf-8", "replace")
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 131072))
            text = f.read().decode("utf-8", "replace")
        age = time.time() - os.path.getmtime(path)
    except Exception:
        return None
    th = None
    for m in re.finditer(r"([\d.]+)\s*T/s", text):
        th = float(m.group(1))
    acc = rej = None
    for m in re.finditer(r"\|\s*(\d+)/(\d+)\(\d+\)\s*\|", text):
        acc, rej = int(m.group(1)), int(m.group(2))
    start = None
    m = re.search(r"\[unix\.(\d+)", head)
    if m:
        start = int(m.group(1))
    if th is None and acc is None:
        return None
    return {"th": th, "acc": acc, "rej": rej, "fresh": age < 45, "start": start}


def build_payload(worker, cfg=None):
    global _online_since
    raw = miner_raw()
    total_hs, devs = miner_normalized(raw)
    hw = gpu_stats()
    total_th = to_th(total_hs)
    acc = rej = None
    log_start = None

    if total_th is None and cfg:
        st = official_log_stats(cfg.get("miner_log"))
        if st and st["fresh"] and st["th"]:
            total_th = st["th"]
            acc, rej = st["acc"], st["rej"]
            log_start = st.get("start")

    online = total_th is not None

    if online:
        if log_start and log_start < time.time():
            uptime = int(time.time() - log_start)
        else:
            if _online_since is None:
                _online_since = time.time()
            uptime = int(time.time() - _online_since)
    else:
        _online_since = None
        uptime = 0

    gpus = []
    for i, g in enumerate(hw):
        d = devs[i] if i < len(devs) else {}
        th = to_th(d.get("hs")) if d else None
        if th is None and i == 0:
            th = total_th
        gpus.append({
            "name": g[0], "temp": g[1], "fan": g[2], "power": g[3],
            "th": th,
            "acc": d.get("acc") if d else acc,
            "rej": d.get("rej") if d else rej,
        })

    powers = [g["power"] for g in gpus if g["power"]]
    temps = [g["temp"] for g in gpus if g["temp"]]
    gst = guardian_state()
    return {
        "ts": int(time.time()),
        "worker": worker,
        "online": online,
        "state": gst.get("state"),
        "game": gst.get("game"),
        "total_th": total_th,
        "power_w": sum(powers) if powers else None,
        "max_temp": max(temps) if temps else None,
        "gpus": gpus,
        "uptime_s": uptime,
    }


def guardian_alive():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        if time.time() - st.get("ts", 0) > 30:
            return False
        out = subprocess.run(["tasklist", "/FI", f"PID eq {st['pid']}", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=NO_WINDOW).stdout
        return str(st["pid"]) in out
    except Exception:
        return False


def spawn_guardian():
    exe = sys.executable or ""
    if exe.lower().endswith("pythonw.exe"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            exe = cand
    subprocess.Popen([exe, GUARDIAN], cwd=BASE,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | NO_WINDOW)


class CtlHandler(BaseHTTPRequestHandler):
    """本机网页控制：GET /ctl?action=start|stop （只监听 127.0.0.1）"""

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._reply(204, {})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path not in ("/ctl", "/"):
            return self._reply(404, {"ok": False, "err": "not found"})
        act = (parse_qs(u.query).get("action") or [""])[0]
        if act == "stop":
            try:
                with open(MANUAL, "w", encoding="utf-8") as f:
                    f.write(str(int(time.time())))
            except Exception as e:
                return self._reply(500, {"ok": False, "err": str(e)})
            return self._reply(200, {"ok": True, "action": "stop", "manual": True})
        if act == "start":
            try:
                os.remove(MANUAL)
            except FileNotFoundError:
                pass
            except Exception as e:
                return self._reply(500, {"ok": False, "err": str(e)})
            if not guardian_alive():
                spawn_guardian()
                time.sleep(1.5)
            return self._reply(200, {"ok": True, "action": "start", "manual": False})
        return self._reply(200, {"ok": True, "manual": os.path.exists(MANUAL),
                                 "guardian": guardian_alive(), **guardian_state()})


def guardian_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        return {"state": st.get("state"), "game": st.get("game"),
                "temp": st.get("temp"), "peak": st.get("peak"),
                "hot": st.get("hot"), "cool": st.get("cool")}
    except Exception:
        return {"state": None, "game": None, "temp": None, "peak": None, "hot": None, "cool": None}


def serve_ctl():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", CTL_PORT), CtlHandler)
        srv.serve_forever()
    except Exception as e:
        print("ctl server fail:", e, flush=True)


def push(cfg, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"], data=body,
        headers={"Content-Type": "application/json", "X-Rig-Token": cfg["token"],
                 "User-Agent": "prl-rig-agent/1.1"},
    )
    with _OPENER.open(req, timeout=15) as r:
        return r.status, r.read(200)



def cmd_endpoint(cfg):
    """远程控制端点：和上报共用 /api 前缀。"""
    base = str(cfg.get("url") or "").rsplit("/", 1)[0]
    return base + "/cmd?w=" + str(cfg.get("worker") or "rig-a")


def _seq_get():
    global _LAST_SEQ
    if _LAST_SEQ is None:
        try:
            with open(CMD_SEQ_FILE, "r", encoding="utf-8") as f:
                _LAST_SEQ = int((f.read() or "0").strip() or 0)
        except Exception:
            _LAST_SEQ = 0
    return _LAST_SEQ


def _seq_set(v):
    global _LAST_SEQ
    _LAST_SEQ = int(v)
    try:
        with open(CMD_SEQ_FILE, "w", encoding="utf-8") as f:
            f.write(str(_LAST_SEQ))
    except Exception:
        pass


def apply_cmd(cfg):
    """执行网页上点下的暂停/恢复命令。

    用 cmd.seq 记录已执行到第几条，避免重启后重放旧命令。
    """
    done = _seq_get()
    try:
        d = http_json(cmd_endpoint(cfg), timeout=8)
    except Exception:
        return
    try:
        seq = int(d.get("seq") or 0)
    except Exception:
        return
    if seq <= done:
        return
    act = d.get("action")
    if act == "stop":
        with open(MANUAL, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        print(f"{time.strftime('%H:%M:%S')} remote: STOP -> manual.pause", flush=True)
    elif act == "start":
        try:
            os.remove(MANUAL)
        except FileNotFoundError:
            pass
        print(f"{time.strftime('%H:%M:%S')} remote: START -> resume", flush=True)
    else:
        return
    _seq_set(seq)


def main():
    cfg = load_cfg()
    if "--debug" in sys.argv:
        raw = miner_raw()
        print("miner api:", json.dumps(raw, ensure_ascii=False)[:2000] if raw else "unreachable")
        print("nvidia-smi:", gpu_stats())
        return
    once = "--once" in sys.argv
    worker = cfg.get("worker", "rig-a")
    print(f"agent -> {cfg['url']} worker={worker}", flush=True)
    if not once:
        threading.Thread(target=serve_ctl, daemon=True).start()
        print(f"ctl listening on 127.0.0.1:{CTL_PORT}", flush=True)
    while True:
        try:
            apply_cmd(cfg)
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')} cmd fail: {type(e).__name__} {e}", flush=True)
        try:
            p = build_payload(worker, cfg)
            code, _ = push(cfg, p)
            print(f"{time.strftime('%H:%M:%S')} push {code} th={p['total_th']} online={p['online']}", flush=True)
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')} push fail: {type(e).__name__} {e}", flush=True)
        if once:
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
