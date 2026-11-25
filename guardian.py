#!/usr/bin/env python3
"""PRL 控制台守护进程
- 可选：自动拉起 SSH 隧道（127.0.0.1:18443 -> 中转 VPS -> 矿池 TLS:443），降低握手延迟
- 自动拉起上报端 rig_agent.py
- 检测到游戏进程时暂停矿工（让出 GPU），游戏退出后自动恢复
- 全部写日志到 guardian.log
"""
import json
import os
import socket
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))   # 本脚本所在目录（可迁移到任意机器）
MINER = os.path.join(BASE, "miner", "miner.exe")
MINER_OUT = os.path.join(BASE, "official.log")
MINER_ERR = os.path.join(BASE, "official.log.err")
AGENT = os.path.join(BASE, "agent", "rig_agent.py")
KEY = os.path.join(BASE, "id_prl")
LOG = os.path.join(BASE, "guardian.log")

WALLET_DEFAULT = ""   # 建议在 agent/config.json 里写 wallet


def rig_identity():
    """矿工名/收款地址从 agent/config.json 读，便于同一套代码部署多台机器。"""
    try:
        with open(os.path.join(BASE, "agent", "config.json"), "r", encoding="utf-8") as f:
            c = json.load(f)
        return (c.get("worker") or "rig-a"), (c.get("wallet") or WALLET_DEFAULT)
    except Exception:
        return "rig-a", WALLET_DEFAULT


WORKER, WALLET = rig_identity()
_cfg = {}
try:
    with open(os.path.join(BASE, "agent", "config.json"), "r", encoding="utf-8") as _f:
        _cfg = json.load(_f)
except Exception:
    _cfg = {}

# 隧道为可选项：配了 tunnel_host 就走 SSH 转发，留空则直连矿池
VPS = _cfg.get("tunnel_host") or ""
LOCAL_PORT = int(_cfg.get("tunnel_local_port") or 18443)
REMOTE = _cfg.get("pool_tls") or "global.pearlfortune.org:443"
PROXY_TARGET = f"127.0.0.1:{LOCAL_PORT}" if VPS else REMOTE

# 命中这些进程就暂停挖矿（大小写不敏感）；可在 games.txt 里自行追加
DEFAULT_GAMES = ["cs2.exe", "TslGame.exe", "PEAK.exe", "valorant.exe", "VALORANT-Win64-Shipping.exe",
                 "r5apex.exe", "gta5.exe", "eldenring.exe", "ForzaHorizon5.exe",
                 "PioneerGame.exe", "DeltaForceClient-Win64-Shipping.exe"]
GAMES_FILE = os.path.join(BASE, "games.txt")


def load_games():
    """默认名单 + games.txt。文件里以 - 开头的行表示「从名单里移除」。"""
    games = list(DEFAULT_GAMES)
    try:
        with open(GAMES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("-") or s.startswith("!"):
                    name = s[1:].strip().lower()
                    games = [g for g in games if g.lower() != name]
                    continue
                if s.lower() not in {g.lower() for g in games}:
                    games.append(s)
    except Exception:
        pass
    return games

POLL = 5              # 秒
RESUME_DELAY = 10     # 游戏退出后等待多少秒再恢复（避免切来切去频繁重启）
LOG_MAX = 1_000_000
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW：避免子进程弹黑框
MANUAL = os.path.join(BASE, "manual.pause")   # 存在 = 用户手动暂停

proc_miner = None
proc_agent = None
proc_tunnel = None
paused_since = None
last_state = None
hot_since = None
cool_since = None
peak_temp = 0.0
last_temp = None
STATE_FILE = os.path.join(BASE, "guardian.state.json")


def cur_temp_now():
    t = gpu_temps()
    return round(max(t), 0) if t else None


def write_state(state, game=None):
    """给桌面控制器/上报端读的状态文件（心跳 5 秒一次）。"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "ts": int(time.time()), "state": state,
                       "temp": (round(last_temp, 0) if last_temp else None), "peak": round(peak_temp, 0),
                       "hot": thermal_cfg()["hot"], "cool": thermal_cfg()["cool"],
                       "game": game, "miner_pid": proc_miner.pid if alive(proc_miner) else None},
                      f, ensure_ascii=False)
    except Exception:
        pass


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def set_state(state):
    global last_state
    if state != last_state:
        last_state = state
        log(f"== state: {state} ==")


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), 1):
            return True
    except OSError:
        return False


def alive(p):
    return p is not None and p.poll() is None


def ensure_tunnel():
    global proc_tunnel
    if not VPS:
        return
    if port_open(LOCAL_PORT):
        return
    if alive(proc_tunnel):
        return
    log("tunnel: (re)starting")
    proc_tunnel = subprocess.Popen(
        ["ssh", "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
         "-o", "ExitOnForwardFailure=yes", "-N",
         "-L", f"127.0.0.1:{LOCAL_PORT}:{REMOTE}", VPS],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )


def ensure_agent():
    global proc_agent
    if alive(proc_agent):
        return
    log("agent: (re)starting")
    proc_agent = subprocess.Popen([sys.executable, AGENT], cwd=os.path.dirname(AGENT),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=0x08000000 if os.name == "nt" else 0)


def game_running():
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout.lower()
    except Exception:
        return None
    for g in load_games():
        if g.lower() in out:
            return g
    return None


# ---------------- 温度保护 ----------------
THERMAL_FILE = os.path.join(BASE, "thermal.pause")
THERMAL_CFG = os.path.join(BASE, "thermal.json")
TH = {"hot": 80, "cool": 70, "hot_sec": 60, "cool_sec": 180}
WARN_TEMP = 75   # 到 75 度在面板/看板上标黄提醒（Windows 下程序改不了风扇）


def thermal_cfg():
    """可在 thermal.json 里改：hot=触发温度, cool=降回多少恢复, hot_sec/cool_sec=持续时间"""
    cfg = dict(TH)
    try:
        with open(THERMAL_CFG, "r", encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except Exception:
        pass
    return cfg


def gpu_temps():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW).stdout
        return [float(x) for x in out.replace("\n", ",").split(",") if x.strip() not in ("", "[N/A]", "N/A")]
    except Exception:
        return []


def thermal_guard():
    """高温持续 hot_sec 秒 -> 写入 thermal.pause（守护随即停矿）；
    降温到 cool 以下持续 cool_sec 秒 -> 删除该文件，自动恢复。"""
    global hot_since, cool_since, peak_temp, last_temp
    cfg = thermal_cfg()
    temps = gpu_temps()
    if not temps:
        return None
    t = max(temps)
    peak_temp = max(peak_temp, t)
    last_temp = t
    now = time.time()
    if t >= cfg["hot"]:
        if hot_since is None:
            hot_since = now
            log(f"thermal: {t:.0f}°C >= {cfg['hot']}°C，计时中")
        elif now - hot_since >= cfg["hot_sec"]:
            if not os.path.exists(THERMAL_FILE):
                try:
                    with open(THERMAL_FILE, "w", encoding="utf-8") as f:
                        f.write(f"{int(now)} {t:.0f}")
                except Exception:
                    pass
                log(f"thermal: 高温保护触发 {t:.0f}°C，暂停挖矿")
        cool_since = None
    else:
        hot_since = None
        if t <= cfg["cool"]:
            if cool_since is None:
                cool_since = now
            elif now - cool_since >= cfg["cool_sec"] and os.path.exists(THERMAL_FILE):
                try:
                    os.remove(THERMAL_FILE)
                except Exception:
                    pass
                log(f"thermal: 已降温 {t:.0f}°C，解除保护，恢复挖矿")
                cool_since = None
        else:
            cool_since = None
    return t


def start_miner():
    global proc_miner
    if alive(proc_miner):
        return
    fake = os.environ.get("PRL_MINER_FAKE") == "1"
    if not fake and VPS and not port_open(LOCAL_PORT):
        log("miner: skip start, tunnel not ready")
        return
    log("miner: start" + (" [FAKE]" if fake else ""))
    if fake:
        proc_miner = subprocess.Popen(["cmd", "/c", "ping -n 600 127.0.0.1"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=0x08000000 if os.name == "nt" else 0)
        set_state("mining [fake]")
        return
    out = open(MINER_OUT, "w", encoding="utf-8", errors="replace")
    err = open(MINER_ERR, "w", encoding="utf-8", errors="replace")
    proc_miner = subprocess.Popen(
        [MINER, "--proxy", PROXY_TARGET, "--address", WALLET, "--worker", WORKER],
        cwd=os.path.dirname(MINER), stdout=out, stderr=err,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    set_state("mining")


def stop_miner(reason):
    global proc_miner, paused_since
    if alive(proc_miner):
        log(f"miner: pause ({reason})")
        try:
            proc_miner.kill()
            proc_miner.wait(timeout=10)
        except Exception:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc_miner.pid)],
                           capture_output=True, creationflags=NO_WINDOW)
        proc_miner = None
    paused_since = time.time()
    set_state(f"paused ({reason})")


def already_running():
    """单实例保护：开机自启和手动启动同时触发时，只跑一个守护。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        pid = st.get("pid")
        if not pid or pid == os.getpid() or time.time() - st.get("ts", 0) > 60:
            return False
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=NO_WINDOW).stdout
        return str(pid) in out
    except Exception:
        return False


def main():
    global paused_since
    if already_running():
        log("guardian: 已在运行，本次退出（单实例保护）")
        return
    log("guardian: start")
    ensure_tunnel()
    ensure_agent()
    current_game = None
    while True:
        try:
            ensure_tunnel()
            ensure_agent()
            g = game_running()
            current_game = g
            cur_temp = thermal_guard()
            if os.path.exists(THERMAL_FILE):
                if alive(proc_miner):
                    stop_miner(f"高温 {cur_temp or 0:.0f}C")
                else:
                    set_state(f"paused (thermal {cur_temp or 0:.0f}C)")
            elif os.path.exists(MANUAL):
                if alive(proc_miner):
                    stop_miner("手动暂停")
                else:
                    set_state("paused (manual)")
            elif g:
                if alive(proc_miner) or paused_since is None:
                    stop_miner(f"game {g}")
                else:
                    set_state(f"paused (game {g})")
            else:
                wait_left = 0 if paused_since is None else RESUME_DELAY - (time.time() - paused_since)
                if wait_left <= 0:
                    if not alive(proc_miner):
                        start_miner()
                        paused_since = None
                else:
                    set_state(f"resume in {int(wait_left)}s")
        except Exception as e:
            log(f"guardian error: {type(e).__name__} {e}")
        write_state(last_state or "starting", current_game)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
