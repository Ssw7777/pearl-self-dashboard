import json, os, sys, time, threading, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("PRL_ROOT") or Path(__file__).resolve().parent.parent)
PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8088))
DATA = ROOT / "data"
TOKEN_FILE = DATA / ".rig_token"
WORKERS_FILE = DATA / "workers.json"

# 每台机器一个目录：data/rigs/<w>.json, data/accrual/<w>.json, data/hist/<w>.jsonl, data/earn/<w>.json
RIGS = DATA / "rigs"
HISTS = DATA / "hist"
ACCS = DATA / "accrual"
ACCH = DATA / "accrual_hourly"
EARNS = DATA / "earn"
EVENTS = DATA / "events"
BARK_KEY_FILE = ROOT / "secrets" / "bark.key"
BARK_GROUP = os.environ.get("BARK_GROUP", "prl")
ALERT_FILE = DATA / "alerts.json"
SUMMARY_SEC = 1800          # 每 30 分钟汇报一次
TEMP_WARN = 75.0            # 温度告警线
TEMP_REALERT = 1800         # 高温持续时，每 30 分钟再提醒一次
OFFLINE_AFTER = 150         # 上报超过 150 秒没更新 = 掉线
CMDS = DATA / "cmd"
CTL_KEY_FILE = ROOT / "secrets" / "ctl.key"
for d in (RIGS, HISTS, ACCS, ACCH, EARNS, EVENTS, CMDS):
    d.mkdir(parents=True, exist_ok=True)

MAX_BODY = 64 * 1024
POOL_MINERS = "https://pearlfortune.org/api/v1/miners?address="
POOL_BLOCKS = "https://pearlfortune.org/api/v1/blocks?worker="
POOL_SUMMARY = "https://pearlfortune.org/api/v1/summary"
CHAIN_ADDR = "https://pearltrack.io/api/v1/address/"
CHAIN_TXS = "https://pearltrack.io/api/v1/address/"

POOL_FEE = 0.05
MINER_FEE = 0.01
ELEC_KW = 0.220
ELEC_CNY = 0.50
USD_CNY = 6.7
ACCT_START = os.environ.get("PRL_ACCT_START", "")   # 统计起点 YYYY-MM-DD，空=不限
NET_TTL = 300
EARN_TTL = 300

WHATTOMINE = "https://whattomine.com/coins/469.json"
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=pearl-2&vs_currencies=usd"

DEFAULT_WORKERS = {
    "rig-a": {"label": "主机 A", "short": "rig-a", "worker": "rig-a", "address": ""},
    "rig-b": {"label": "主机 B", "short": "rig-b", "worker": "rig-b", "address": ""},
}


def workers():
    try:
        d = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d:
            return d
    except Exception:
        pass
    try:
        WORKERS_FILE.write_text(json.dumps(DEFAULT_WORKERS, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return DEFAULT_WORKERS


def read_token():
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def ctl_key():
    """控制密码：优先读 secrets/ctl.key，没有就自动生成并打印。"""
    try:
        k = CTL_KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    except Exception:
        pass
    try:
        import secrets as _s
        k = _s.token_urlsafe(24)
        CTL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        CTL_KEY_FILE.write_text(k, encoding="utf-8")
        try:
            os.chmod(CTL_KEY_FILE, 0o600)
        except Exception:
            pass
        print("=== 已生成远程控制密码 secrets/ctl.key ===", flush=True)
        print("    " + k, flush=True)
        return k
    except Exception:
        return ""


def read_cmd(w):
    """某台机器待执行的远程命令（网页上点暂停/恢复写在这里）"""
    try:
        return json.loads((CMDS / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        return {"seq": 0, "action": None, "ts": 0}


def write_cmd(w, action):
    cur = read_cmd(w)
    obj = {"seq": int(cur.get("seq") or 0) + 1, "action": action,
           "ts": int(time.time()), "by": "web"}
    tmp = CMDS / (w + ".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CMDS / (w + ".json"))
    return obj


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; prl-rig/2.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


NET_CACHE = DATA / "net_cache.json"


def refresh_net(force=False):
    now = int(time.time())
    cached = {}
    try:
        cached = json.loads(NET_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cached = {}
    if not force and cached.get("updated", 0) and now - cached["updated"] < NET_TTL:
        return cached
    net = dict(cached)
    try:
        d = _get_json(WHATTOMINE)
        if d.get("nethash"):
            net["eh"] = float(d["nethash"]) / 1e18
        if d.get("block_reward"):
            net["reward"] = float(d["block_reward"])
        if d.get("block_time"):
            net["btime"] = float(d["block_time"])
    except Exception as e:
        print("whattomine fail:", e, flush=True)
    try:
        g = _get_json(COINGECKO)
        p = ((g.get("pearl-2") or {}).get("usd"))
        if p:
            net["price"] = float(p)
    except Exception as e:
        print("coingecko fail:", e, flush=True)
    net["updated"] = now
    try:
        NET_CACHE.write_text(json.dumps(net), encoding="utf-8")
    except Exception:
        pass
    return net


def acct_start_ts():
    try:
        import calendar
        return calendar.timegm(time.strptime(ACCT_START + " 00:00:00", "%Y-%m-%d %H:%M:%S")) - 8 * 3600
    except Exception:
        return 0


def collect_earnings(w):
    cfg = workers().get(w) or {}
    addr = cfg.get("address") or ""
    wname = cfg.get("worker") or w
    out = {}
    try:
        out = json.loads((EARNS / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        out = {}
    try:
        m = _get_json(POOL_MINERS + addr)
        rows = (m.get("data") or {}).get("balances") or []
        out["pool_balance_prl"] = (rows[0].get("balance_atomic") or 0) / 1e8 if rows else 0.0
        out["pool_has_record"] = bool(rows)
    except Exception as e:
        print("pool miners fail:", w, e, flush=True)
    try:
        b = _get_json(POOL_BLOCKS + wname + "&page_size=20")
        d = b.get("data") or {}
        blocks = d.get("blocks") or []
        out["blocks_total"] = d.get("total")
        out["blocks_reward_prl"] = sum(float(x.get("block_reward") or 0) for x in blocks)
    except Exception as e:
        print("pool blocks fail:", w, e, flush=True)
    try:
        a = _get_json(CHAIN_ADDR + addr)
        out["onchain_balance_prl"] = a.get("balancePrl")
        out["onchain_received_prl"] = a.get("totalReceivedPrl")
        out["onchain_txs"] = a.get("txCount")
    except Exception as e:
        print("pearltrack fail:", w, e, flush=True)
    if not addr:
        out["payouts"] = []
        return
    try:
        tx = _get_json(CHAIN_TXS + addr + "/txs?limit=40")
        rows = tx.get("txs") or []
        pays = []
        cut = acct_start_ts()
        for t in rows:
            if (t.get("timeMs") or 0) / 1000 < cut:
                continue
            if t.get("type") in ("receive", "coinbase") and (t.get("receivedGrains") or 0) > 0:
                pays.append({"ts": int((t.get("timeMs") or 0) / 1000),
                             "prl": round((t.get("receivedGrains") or 0) / 1e8, 6),
                             "height": t.get("blockHeight"),
                             "txid": t.get("txid")})
        out["payouts"] = pays[:20]
        out["payout_total_prl"] = round(sum(x["prl"] for x in pays), 6)
        out["payout_count"] = len(pays)
        out["last_payout_ts"] = pays[0]["ts"] if pays else None
        out["last_payout_prl"] = pays[0]["prl"] if pays else None
    except Exception as e:
        print("payout txs fail:", w, e, flush=True)
    try:
        s = _get_json(POOL_SUMMARY)
        st = ((s.get("data") or {}).get("stats")) or {}
        out["network_hashrate"] = st.get("network_hashrate")
        out["chain_height"] = st.get("latest_height")
    except Exception as e:
        print("pool summary fail:", w, e, flush=True)
    out["address"] = addr
    out["worker_name"] = wname
    out["updated"] = int(time.time())
    try:
        (EARNS / (w + ".json")).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def accrue(w, dt):
    try:
        rig = json.loads((RIGS / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        return
    if not _rig_fresh(rig):
        return
    if not rig.get("online") or not rig.get("est_day_prl"):
        return
    day = time.strftime("%Y-%m-%d")
    p = ACCS / (w + ".json")
    try:
        acc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        acc = {}
    add = float(rig["est_day_prl"]) * min(dt, 300) / 86400.0
    acc[day] = round(acc.get(day, 0) + add, 6)
    for k in list(acc.keys())[:-60]:
        acc.pop(k, None)
    try:
        p.write_text(json.dumps(acc), encoding="utf-8")
    except Exception:
        pass
    ph = ACCH / (w + ".json")
    try:
        hour = json.loads(ph.read_text(encoding="utf-8"))
    except Exception:
        hour = {}
    hk = str(int(time.time() // 3600 * 3600))
    hour[hk] = round(hour.get(hk, 0) + add, 6)
    cutoff = int(time.time()) - 72 * 3600
    hour = {k: v for k, v in hour.items() if int(k) >= cutoff}
    try:
        ph.write_text(json.dumps(hour), encoding="utf-8")
    except Exception:
        pass


STALE_SEC = 150   # 上报超过这么久没更新 = 视为离线（关机/断网）


def _rig_fresh(rig, now=None):
    """判断这条上报是否还新鲜。不新鲜就不能按它的算力记账。"""
    try:
        ts = float(rig.get("ts") or 0)
    except Exception:
        return False
    now = now or time.time()
    return ts > 0 and (now - ts) <= STALE_SEC


def sample_history(w):
    try:
        rig = json.loads((RIGS / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        return
    now = time.time()
    fresh = _rig_fresh(rig, now)
    row = {"t": int(now),
           "th": round(float(rig.get("total_th") or 0), 2) if fresh else 0,
           "pw": round(float(rig.get("power_w") or 0), 0) if fresh else 0,
           "tc": round(float(rig.get("max_temp") or 0), 0) if fresh else 0,
           "on": 1 if (fresh and rig.get("online")) else 0}
    p = HISTS / (w + ".jsonl")
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        if p.stat().st_size > 3_000_000:
            lines = p.read_text(encoding="utf-8").splitlines()[-20000:]
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def history_view(w, hours):
    now = time.time()
    start = now - hours * 3600
    rows = []
    try:
        with open(HISTS / (w + ".jsonl"), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("t", 0) >= start:
                    rows.append(r)
    except Exception:
        pass
    if not rows:
        return []
    buckets = min(240, max(1, len(rows)))
    span = max(1.0, now - start)
    acc = {}
    for r in rows:
        idx = min(buckets - 1, int((r["t"] - start) / span * buckets))
        a = acc.setdefault(idx, [0.0, 0.0])
        a[0] += r.get("th") or 0
        a[1] += 1
    return [{"t": int(start + span * (i + 0.5) / buckets), "th": round(acc[i][0] / acc[i][1], 2)}
            for i in sorted(acc)]


MIN_EVENT = 600   # 只记录持续 >= 10 分钟的状态


def rebuild_events(w):
    """从采样历史重建活动记录：算力>0=挖矿中，算力=0但有上报=暂停，没上报=关机。"""
    hp = HISTS / (w + ".jsonl")
    try:
        rows = []
        with open(hp, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return
    if not rows:
        return
    segs = []
    cur = None
    for r in rows:
        th = r.get("th") or 0
        on = r.get("on")
        if th and th > 0:
            st = "mining"
        elif (r.get("pw") or 0) > 0 or (r.get("tc") or 0) > 0:
            st = "paused"          # 机器还开着、有功耗/温度，只是没在挖（打游戏/手动暂停/高温保护）
        else:
            st = "offline"         # 功耗温度全 0 = 关机/断网
        t = int(r.get("t") or 0)
        if not t:
            continue
        if cur and cur["state"] == st:
            cur["end"] = t
        else:
            if cur:
                segs.append(cur)
            cur = {"start": t, "end": t, "state": st}
    if cur:
        segs.append(cur)
    segs = [x for x in segs if x["end"] - x["start"] >= MIN_EVENT]
    out = {"cur": segs[-1]["state"] if segs else None, "log": segs[-200:], "rebuilt": int(time.time())}
    p = EVENTS / (w + ".json")
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass



def events_view(w, limit=20):
    try:
        st = json.loads((EVENTS / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        return []
    log = st.get("log") or []
    out = list(reversed(log[-limit:]))
    return out


def _bark_key():
    try:
        return BARK_KEY_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def bark(title, body):
    """发 Bark 推送。没配 key 就静默跳过。"""
    key = _bark_key()
    if not key:
        return False
    from urllib.parse import quote
    url = (f"https://api.day.app/{key}/{quote(str(title), safe='')}/"
           f"{quote(str(body), safe='')}?group={BARK_GROUP}&ttl=315360000")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            ok = r.status == 200
        print(f"bark: {title} -> {ok} | {str(body)[:120].replace(chr(10), " / ")}", flush=True)
        return ok
    except Exception as e:
        print("bark fail:", e, flush=True)
        return False


def _alerts_load():
    try:
        return json.loads(ALERT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _alerts_save(d):
    try:
        tmp = ALERT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ALERT_FILE)
    except Exception:
        pass


def _state_key(rig):
    """状态归一化，用于去重：paused (manual) / paused (手动暂停) 视为同一种。"""
    s = (rig.get("state") or "").lower()
    if not s:
        return "unknown"
    if "mining" in s:
        return "mining"
    if "thermal" in s or "高温" in s:
        return "thermal"
    if "manual" in s or "手动" in s:
        return "manual"
    if "game" in s or "游戏" in s:
        return "game"
    return "other"


def _rig_of(w):
    try:
        return json.loads((RIGS / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _state_cn(st, rig):
    s = (st or "").lower()
    if not rig:
        return "无数据"
    if "thermal" in s:
        return "高温保护中"
    if "manual" in s:
        return "手动暂停"
    if "game" in s:
        return "游戏暂停"
    if "mining" in s:
        return "挖矿中"
    return st or "未知"


def _state_word(rig, alive):
    if not alive:
        return "掉线"
    s = (rig.get("state") or "").lower()
    if "mining" in s:
        return "进行中"
    if "thermal" in s:
        return "高温保护"
    if "manual" in s:
        return "已暂停"
    if "game" in s:
        return "游戏暂停"
    return "未知"


def summary_text():
    now = time.time()
    ws = workers()
    total_th = 0.0
    online = 0
    lines = ["", ]
    p1 = p6 = p24 = 0.0
    for wid, info in ws.items():
        rig = _rig_of(wid)
        th = float(rig.get("total_th") or 0)
        alive = bool(rig) and (now - float(rig.get("ts") or 0)) < OFFLINE_AFTER
        if alive:
            online += 1
            total_th += th
        name = info.get("short") or info.get("label") or wid
        if alive:
            lines.append(f"{name}: {th:.1f}T----{float(rig.get('max_temp') or 0):.0f}°C"
                         f"----{float(rig.get('power_w') or 0):.0f}W----{_state_word(rig, True)}")
        else:
            mins = int((now - float(rig.get("ts") or 0)) / 60) if rig.get("ts") else 0
            lines.append(f"{name}: 掉线" + (f"（{mins} 分钟没上报）" if mins > 0 else ""))
        try:
            p1 += hist_last(wid, 1)
            p6 += hist_last(wid, 6)
            p24 += hist_last(wid, 24)
        except Exception:
            pass
    lines.append(f"过去1h {p1:.4f}  过去6h {p6:.4f}  过去24h {p24:.4f} PRL")
    head = f"PRL 数据  {total_th:.1f} TH/s · {online}/{len(ws)} 在线"
    return head, "\n".join(lines)


def notify_tick(first=False):
    """每 30 秒调一次：处理定期汇报 + 三类告警。"""
    now = time.time()
    st = _alerts_load()
    st.setdefault("last_summary", 0)
    st.setdefault("workers", {})

    # ---- 定期汇报 ----
    if first or (now - float(st.get("last_summary") or 0) >= SUMMARY_SEC):
        head, body = summary_text()
        if bark(head, body):
            st["last_summary"] = now
            _alerts_save(st)

    # ---- 逐台检查 ----
    changed = False
    for wid, info in workers().items():
        rig = _rig_of(wid)
        label = info.get("label") or wid
        wst = st["workers"].setdefault(wid, {})
        ts = float(rig.get("ts") or 0)
        alive = bool(ts) and (now - ts) < OFFLINE_AFTER
        prev_alive = wst.get("alive")

        if prev_alive is None:
            wst["alive"] = alive
            changed = True
        elif prev_alive and not alive:
            age = int(now - ts) if ts else 0
            if bark(f"⚠️ {label} 掉线", f"已 {age} 秒没有上报\n算力最后 {float(rig.get('total_th') or 0):.1f} TH/s"):
                wst["alive"] = False
                wst["offline_at"] = now
                changed = True
        elif (not prev_alive) and alive:
            if bark(f"✅ {label} 恢复在线", f"算力 {float(rig.get('total_th') or 0):.1f} TH/s"):
                wst["alive"] = True
                wst.pop("offline_at", None)
                changed = True

        if not alive:
            continue

        # ---- 高温 ----
        temp = float(rig.get("max_temp") or 0)
        if temp >= TEMP_WARN:
            last = float(wst.get("temp_alert_at") or 0)
            if now - last >= TEMP_REALERT:
                if bark(f"🌡️ {label} 温度 {temp:.0f}°C",
                        f"超过 {TEMP_WARN:.0f}°C 警戒线\n算力 {float(rig.get('total_th') or 0):.1f}T "
                        f"功耗 {float(rig.get('power_w') or 0):.0f}W\n到 80°C 会自动停矿"):
                    wst["temp_alert_at"] = now
                    changed = True
        else:
            if wst.get("temp_alert_at") and temp > 0 and temp < TEMP_WARN - 3:
                wst["temp_alert_at"] = 0
                changed = True

        # ---- 暂停（手动 / 高温保护 / 游戏）----
        cur_key = _state_key(rig)
        cur = _state_cn(rig.get("state"), rig)
        prev = wst.get("state_key")
        if prev != cur_key:
            if prev is not None and cur_key not in ("mining", "unknown"):
                bark(f"⏸️ {label} {cur}", f"算力 {float(rig.get('total_th') or 0):.1f}T · 温度 {temp:.0f}°C")
            elif prev is not None and cur_key == "mining" and prev != "mining":
                bark(f"▶️ {label} 恢复挖矿", f"算力 {float(rig.get('total_th') or 0):.1f} TH/s")
            wst["state_key"] = cur_key
            wst["state"] = cur
            changed = True

    if changed:
        _alerts_save(st)


def background():
    last = 0
    prev = time.time()
    tick = 0
    while True:
        now = time.time()
        dt = now - prev
        prev = now
        try:
            for w in workers():
                accrue(w, dt)
                rebuild_events(w)
            notify_tick(first=(tick == 0))
            tick += 1
            if tick % 2 == 0:
                for w in workers():
                    sample_history(w)
            if now - last > EARN_TTL:
                for w in workers():
                    collect_earnings(w)
                last = now
        except Exception as e:
            print("bg fail:", e, flush=True)
        time.sleep(30)


def enrich(obj):
    cfg = workers().get(obj.get("worker") or "rig-a") or {}
    net = refresh_net()
    th = obj.get("total_th")
    if th and net.get("eh") and net.get("btime") and net.get("reward"):
        share = (th * 1e12) / (net["eh"] * 1e18)
        gross = (86400.0 / net["btime"]) * net["reward"] * share
        obj["gross_day_prl"] = gross
        obj["est_day_prl"] = gross * (1 - POOL_FEE) * (1 - MINER_FEE)
    obj["net_eh"] = net.get("eh")
    obj["block_reward"] = net.get("reward")
    obj["block_time"] = net.get("btime")
    obj["price_usd"] = net.get("price")
    obj["elec_cny_day"] = ELEC_KW * 24 * ELEC_CNY
    if obj.get("est_day_prl") and obj.get("price_usd"):
        obj["profit_cny_day"] = obj["est_day_prl"] * obj["price_usd"] * USD_CNY - obj["elec_cny_day"]
    return obj



def hist_last(w, hours):
    """按算力历史积分，算最近 N 小时的产出（PRL）。"""
    net = refresh_net()
    eh, btime, reward = net.get("eh"), net.get("btime"), net.get("reward")
    if not (eh and btime and reward):
        return 0.0
    per_th_sec = (1.0 / btime) * reward * (1e12 / (eh * 1e18)) * (1 - POOL_FEE) * (1 - MINER_FEE)
    now = time.time()
    start = now - float(hours) * 3600
    rows = []
    try:
        with open(HISTS / (w + ".jsonl"), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("t", 0) >= start - 60:
                    rows.append(r)
    except Exception:
        return 0.0
    if len(rows) < 2:
        return 0.0
    total = 0.0
    for a, b in zip(rows, rows[1:]):
        dt = max(0, min(120, b["t"] - a["t"]))
        if dt <= 0:
            continue
        th = ((a.get("th") or 0) + (b.get("th") or 0)) / 2.0
        total += th * per_th_sec * dt
    tail = max(0, min(120, now - rows[-1]["t"]))
    total += (rows[-1].get("th") or 0) * per_th_sec * tail
    return total


def hist_last24(w):
    """用算力历史积分反推过去 24 小时的产出（比从零累积更准，且立刻可用）。"""
    net = refresh_net()
    eh, btime, reward = net.get("eh"), net.get("btime"), net.get("reward")
    if not (eh and btime and reward):
        return None
    per_th_sec = (1.0 / btime) * reward * (1e12 / (eh * 1e18)) * (1 - POOL_FEE) * (1 - MINER_FEE)
    now = time.time()
    start = now - 24 * 3600
    rows = []
    try:
        with open(HISTS / (w + ".jsonl"), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("t", 0) >= start - 60:
                    rows.append(r)
    except Exception:
        return None
    if len(rows) < 2:
        return 0.0
    total = 0.0
    for a, b in zip(rows, rows[1:]):
        dt = max(0, min(120, b["t"] - a["t"]))
        if dt <= 0:
            continue
        th = ((a.get("th") or 0) + (b.get("th") or 0)) / 2.0
        total += th * per_th_sec * dt
    # 加上最后一个采样点到现在的空档
    tail = max(0, min(120, now - rows[-1]["t"]))
    total += (rows[-1].get("th") or 0) * per_th_sec * tail
    return round(total, 4)


_COV_CACHE = {}


def day_coverage(w):
    """每天实际有数据的起止时间（用于在每日产出里标注 0点~几点）。"""
    hp = HISTS / (w + ".jsonl")
    try:
        mt = hp.stat().st_mtime
    except Exception:
        return {}
    ck = (w, mt)
    if ck in _COV_CACHE:
        return _COV_CACHE[ck]
    cov = {}
    try:
        with open(hp, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts = r.get("t") or 0
                if not ts:
                    continue
                d = time.strftime("%Y-%m-%d", time.localtime(ts))
                c = cov.get(d)
                if c is None:
                    cov[d] = [int(ts), int(ts)]
                else:
                    if ts < c[0]:
                        c[0] = int(ts)
                    if ts > c[1]:
                        c[1] = int(ts)
    except Exception:
        return {}
    if len(_COV_CACHE) > 40:
        _COV_CACHE.clear()
    _COV_CACHE[ck] = cov
    return cov


def earnings_view(w):
    try:
        e = json.loads((EARNS / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        e = {}
    try:
        acc = json.loads((ACCS / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        acc = {}
    days = sorted(acc.items())[-14:]
    e["accrual"] = [{"date": k, "prl": round(v, 4)} for k, v in days]
    e["today_prl"] = round(acc.get(time.strftime("%Y-%m-%d"), 0), 4)
    try:
        hour = json.loads((ACCH / (w + ".json")).read_text(encoding="utf-8"))
    except Exception:
        hour = {}
    cut = int(time.time()) - 24 * 3600
    e["last24_prl_hourly"] = round(sum(v for k, v in hour.items() if int(k) >= cut - 3600), 4)
    hv = hist_last24(w)
    e["last24_prl"] = hv if hv is not None else e["last24_prl_hourly"]
    e["total_accrued_prl"] = round(sum(acc.values()), 4)
    cfg = workers().get(w) or {}
    e["label"] = cfg.get("label")
    e["address"] = cfg.get("address")
    e["price_usd"] = refresh_net().get("price")
    e["coverage"] = day_coverage(w)
    return e


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(Path(__file__).resolve().parent / "static"), **k)
    def end_headers(self):
        """HTML 一律禁缓存，避免 iframe / 浏览器拿旧版本。"""
        p = self.path.split("?", 1)[0]
        if p.endswith(".html") or p in ("/", ""):
            self.send_header("Cache-Control", "no-store, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()


    def _q(self):
        return self.path.split("?", 1)[1] if "?" in self.path else ""

    def _param(self, name, default=""):
        for part in self._q().split("&"):
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return default

    def _rewrite(self):
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            self.path = "/index.html"

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/api/cmd":
            w = self._param("w", "rig-a")
            if w not in workers():
                return self._send(403, b'{"ok":false,"err":"unknown worker"}')
            return self._send(200, json.dumps(read_cmd(w)).encode())
        if p == "/api/rig":
            w = self._param("w", "rig-a")
            try:
                body = (RIGS / (w + ".json")).read_bytes()
                json.loads(body)
            except Exception:
                body = b'{"online":false,"ts":0}'
            return self._send(200, body)
        if p == "/api/net":
            return self._send(200, json.dumps(refresh_net()).encode())
        if p == "/api/earnings":
            return self._send(200, json.dumps(earnings_view(self._param("w", "rig-a")),
                                              ensure_ascii=False).encode())
        if p == "/api/history":
            try:
                hours = max(0.1, min(168.0, float(self._param("h", "1"))))
            except Exception:
                hours = 1.0
            return self._send(200, json.dumps({"hours": hours,
                                               "points": history_view(self._param("w", "rig-a"), hours)}).encode())
        if p == "/api/events":
            try:
                lim = int(self._param("limit", "20"))
            except Exception:
                lim = 20
            return self._send(200, json.dumps({"events": events_view(self._param("w", "rig-a"), lim)},
                                              ensure_ascii=False).encode())
        if p == "/api/workers":
            ws = workers()
            return self._send(200, json.dumps([{"id": k, "label": v.get("label"), "address": v.get("address")}
                                               for k, v in ws.items()], ensure_ascii=False).encode())
        self._rewrite()
        return super().do_GET()

    def do_HEAD(self):
        self._rewrite()
        return super().do_HEAD()

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _api_rig_post(self):
        token = read_token()
        if not token or self.headers.get("X-Rig-Token", "") != token:
            return self._send(401, b'{"ok":false,"err":"bad token"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._send(400, b'{"ok":false,"err":"bad length"}')
        try:
            obj = json.loads(self.rfile.read(n))
        except Exception:
            return self._send(400, b'{"ok":false,"err":"bad json"}')
        w = (obj.get("worker") or "rig-a").strip() or "rig-a"
        ws = workers()
        if w not in ws:
            ws[w] = {"label": obj.get("label") or w, "worker": w,
                     "address": obj.get("wallet") or DEFAULT_WORKERS["rig-a"]["address"]}
            try:
                WORKERS_FILE.write_text(json.dumps(ws, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            print("worker auto-registered:", w, flush=True)
        try:
            obj = enrich(obj)
        except Exception as e:
            print("enrich fail:", e, flush=True)
        obj["received_at"] = int(time.time())
        tmp = RIGS / (w + ".json.tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, RIGS / (w + ".json"))
        return self._send(200, b'{"ok":true}')

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/api/cmd":
            return self._api_cmd_post()
        if self.path.split("?", 1)[0] == "/api/rig":
            return self._api_rig_post()
        return self._send(404, b'{"ok":false}')

    def _api_cmd_post(self):
        """网页暂停/恢复：必须带 X-Ctl-Key，否则 401。"""
        key = ctl_key()
        if not key or self.headers.get("X-Ctl-Key", "") != key:
            return self._send(401, b'{"ok":false,"err":"bad key"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._send(400, b'{"ok":false,"err":"bad length"}')
        try:
            obj = json.loads(self.rfile.read(n))
        except Exception:
            return self._send(400, b'{"ok":false,"err":"bad json"}')
        w = (obj.get("worker") or "").strip()
        act = (obj.get("action") or "").strip()
        if w not in workers():
            return self._send(403, b'{"ok":false,"err":"unknown worker"}')
        if act not in ("stop", "start"):
            return self._send(400, b'{"ok":false,"err":"bad action"}')
        out = write_cmd(w, act)
        print("cmd:", w, act, "seq", out["seq"], flush=True)
        return self._send(200, json.dumps({"ok": True, **out}).encode())

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)


workers()
ctl_key()   # 启动时就生成控制密码
threading.Thread(target=background, daemon=True).start()
print(f"Pearl dashboard http://0.0.0.0:{PORT}/  (root={ROOT})", flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()