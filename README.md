# Pearl Self Dashboard · PRL 多机挖矿自建看板

一套**自托管**的 Pearl (PRL) 挖矿监控与控制系统：Web 看板 + Windows 桌面控制台 + 守护进程。
覆盖设备管理、产出统计、温度保护、异常恢复与远程控制，
适配多设备集群、多卡矿机，也适配单台自用游戏电脑（闲置自动挖矿、开机即恢复）。

> Self-hosted monitoring & control for Pearl (PRL) GPU mining.
> Web dashboard + Windows desktop console + supervisor daemon.
> Per-rig accounting, thermal protection, remote start/stop from the browser.

**适用场景**：多设备统一管理 · 多卡矿机 · 自用游戏电脑（闲置自动挖矿、开机即恢复）

---

## 目录

1. [功能清单（按需选）](#1-功能清单按需选)
2. [架构](#2-架构)
3. [目录结构](#3-目录结构)
4. [完整教程](#4-完整教程)
5. [配置参考](#5-配置参考)
6. [HTTP API](#6-http-api)
7. [数据口径](#7-数据口径)
8. [常见问题](#8-常见问题)
9. [安全说明](#9-安全说明)
10. [许可](#10-许可)

---

## 1. 功能清单（按需选）

**不是所有功能都必须装。** 先看这张表，勾出你需要的，再按对应章节做。

| # | 功能 | 作用 | 必需？ | 默认 |
|---|---|---|---|---|
| F1 | **看板服务** | 网页上看所有机器的算力/温度/收益 | ✅ 必需 | 开 |
| F2 | **机器上报端** | 每台机器把数据推到看板 | ✅ 必需 | 开 |
| F3 | **守护进程** | 矿工崩了自动重启、断线自动重连 | ⭐ 强烈建议 | 开 |
| F4 | **桌面控制台** | Windows 上的原生窗口，看数据 + 一键开关 | ⭕ 可选 | 开 |
| F5 | **温度保护** | 超过阈值自动停矿，降温自动恢复 | ⭕ 可选 | 开（80/70℃） |
| F6 | **游戏自动暂停** | 检测到游戏就停矿，退出后恢复 | ⭕ 可选 | 开 |
| F7 | **网页远程控制** | 在网页上点按钮暂停/恢复任意一台机器 | ⭕ 可选 | 关（需配密码） |
| F8 | **多机独立记账** | 每台设备各自累计产出，互不干扰 | ⭕ 可选 | 开（支持多台即生效） |
| F9 | **打款记录** | 从链上抓矿池转入地址的每一笔 | ⭕ 可选 | 开 |
| F10 | **SSH 隧道加速** | 跨境线路握手太慢时，经 VPS 中转 | ⭕ 可选 | 关（留空=直连） |
| F11 | **开机自启** | 开机自动开始挖 | ⭕ 可选 | 关 |
| F12 | **域名 + HTTPS** | 用自己的域名访问看板 | ⭕ 可选 | 关 |
| F13 | **只读分享页** | 无控制按钮的纯展示页面 | ⭕ 可选 | 关 |
| F14 | **Bark 手机推送** | 每 30 分钟汇报 + 高温/掉线/暂停告警 | ⭕ 可选 | 关（需填 key） |
| F15 | **实时币价** | 页面顶部显示 PRL 美元价 + 折合人民币 | ⭕ 可选 | 开 |
| F16 | **活动记录** | 挖矿 / 暂停 / 关机 三种状态，超 10 分钟才记，10 条一页 | ⭕ 可选 | 开 |

> 想让人/AI 帮你逐项决定，直接看 [`docs/AI-SETUP.md`](docs/AI-SETUP.md)，
> 那里有一份可以整段复制给 AI 的问询清单，它会一项项问你然后生成配置。

---

## 2. 架构

```
┌──────────── 每台挖矿机（Windows）────────────┐
│                                              │
│  guardian.py ──启动/看护──▶ miner.exe        │
│      │                        │              │
│      │                        └─▶ 矿池 TLS   │
│      │                                       │
│      ├──▶ agent/rig_agent.py ──每 N 秒 POST──┼──┐
│      │      （nvidia-smi + 矿工日志）        │  │
│      │      ◀── 每 N 秒 GET 命令 ────────────┼──┤
│      └──▶ panel.py（桌面控制台）             │  │
└──────────────────────────────────────────────┘  │
                                                  │ HTTPS
                                                  ▼
                    ┌──────────────────────────────────┐
                    │ dashboard/server.py（你的服务器）│
                    │  /api/rig      接收上报           │
                    │  /api/cmd      下发暂停/恢复      │
                    │  /api/earnings 产出与收益         │
                    │  /api/history  曲线               │
                    │  /api/events   在线/离线事件      │
                    │  /              看板页面          │
                    └──────────────────────────────────┘
```

**关键设计**：矿机**不开任何入站端口**，全程主动往外拉（上报数据 + 拉命令）。
所以不需要公网 IP、不需要端口映射、不需要内网穿透。

---

## 3. 目录结构

```
pearl-self-dashboard/
├── guardian.py                 守护进程（拉起矿工/上报端/隧道、温度保护、游戏暂停）
├── panel.py                    Windows 桌面控制台（tkinter）
├── pearlctl.py                 命令行开关：start / stop / status
├── games.txt                   游戏进程名单
├── thermal.json                温度阈值
├── agent/
│   ├── rig_agent.py            上报端 + 远程命令轮询
│   └── config.example.json     配置模板
├── dashboard/
│   ├── server.py               看板服务（纯标准库，零第三方依赖）
│   ├── run.sh                  启动脚本
│   └── static/index.html       看板页面（单文件，内联 CSS/JS）
├── install/
│   └── install.ps1             Windows 一键安装
├── docs/
│   ├── AI-SETUP.md             给 AI 用的配置问询清单
│   └── TROUBLESHOOTING.md      故障排查
├── LICENSE
└── README.md
```

---

## 4. 完整教程

### 4.0 准备

| 项目 | 要求 | 检查命令 |
|---|---|---|
| 挖矿机 | Windows 10/11 + NVIDIA 显卡 + 驱动 | `nvidia-smi` |
| Python | 3.10+（带 tkinter） | `python --version` |
| 服务器 | 任意 Linux / Windows，能跑 Python 3 | `python3 --version` |
| 矿工 | 矿池官方 `pfminer`（**本仓库不附带**） | 见下方说明 |

> **矿工程序不在本仓库里**。请到矿池官网的矿工下载页获取对应系统的版本，
> 解压后把 `miner.exe` 放到项目下的 `miner/` 目录。

### 4.1 部署看板服务（F1）

**Linux**

```bash
cd pearl-self-dashboard/dashboard
bash run.sh 8088
```

**Windows**

```powershell
cd pearl-self-dashboard\dashboard
python server.py 8088
```

首次启动会打印两行重要信息，**记下来**：

```
=== 已自动生成远程控制密码（secrets/ctl.key）===
    xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

访问 `http://你的服务器:8088/` 应该能看到一个空的看板页面。

### 4.2 配置第一台矿机（F2 + F3）

```powershell
cd C:\path\to\pearl-self-dashboard
copy agent\config.example.json agent\config.json
notepad agent\config.json
```

最少填这 4 项：

```json
{
  "worker": "rig-a",
  "wallet": "prl1...你的收款地址",
  "url": "http://你的服务器:8088/api/rig",
  "token": "看板启动时生成的上报令牌"
}
```

> 上报令牌在哪？看板服务端启动时会在 `data/.rig_token` 生成，
> 也可以在服务器上 `cat data/.rig_token` 查看。

### 4.3 启动并验证

```powershell
python guardian.py
```

观察日志（`guardian.log`）：

```
guardian: start
tunnel: skip (direct)
miner: start
agent: (re)starting
```

30 秒后刷新看板，应该出现这台机器。

**验证清单**

- [ ] 看板上出现机器卡片，算力不是 0
- [ ] 温度、功耗、风扇有数值
- [ ] `guardian.state.json` 里 `state` 是 `mining`

### 4.4 加第二台机器（F8）

复制整个目录到第二台机器，**只改 `worker` 名字**（比如 `rig-b`），其余不变。
看板会自动发现并建立独立账本，不需要在服务器上登记。

### 4.5 开启网页远程控制（F7，可选）

**服务器侧**：不用改，`/api/cmd` 已经内置。把启动时打印的控制密码找出来：

```bash
cat secrets/ctl.key
```

**矿机侧**：确认 `agent/rig_agent.py` 里有 `apply_cmd`（本仓库自带），然后重启守护。

**使用**：看板顶部「远程控制」条 → 选机器 → 输密码 → 点「■ 暂停」/「▶ 恢复」。

**验证**

```bash
# 队列状态
curl 'http://你的服务器:8088/api/cmd?w=rig-a'
# 发一条暂停
curl -X POST 'http://你的服务器:8088/api/cmd' \
  -H 'Content-Type: application/json' -H 'X-Ctl-Key: 你的密码' \
  -d '{"worker":"rig-a","action":"stop"}'
```

10 秒内矿机侧 `manual.pause` 会出现，矿工停止；再发 `start` 就恢复。

> 远程暂停 = 面板上点「停止挖矿」，写的是同一个 `manual.pause` 文件。
> 游戏暂停和高温保护走独立状态，**三者互不干扰**。

### 4.6 温度保护（F5，可选）

编辑 `thermal.json`：

```json
{ "hot": 80, "cool": 70, "hot_sec": 60, "cool_sec": 180 }
```

- 温度 ≥ `hot` 持续 `hot_sec` 秒 → 自动停矿
- 温度 ≤ `cool` 持续 `cool_sec` 秒 → 自动恢复

改完 10 秒内生效，不用重启。

> ⚠️ Windows 下**软件控制不了 NVIDIA 消费卡的风扇**
> （官方矿工 `--gpu-fan` 会报 `overclock settings are not supported on Windows`，
> `nvidia-smi` 也没有风扇接口）。要风扇曲线请用显卡厂商工具。

### 4.7 游戏自动暂停（F6，可选）

编辑 `games.txt`，一行一个进程名：

```
cs2.exe
TslGame.exe
valorant.exe
```

以 `-` 开头表示**从默认名单移除**：

```
-PEAK.exe     # 这个程序运行时不要暂停挖矿
```

### 4.8 SSH 隧道加速（F10，可选）

跨境直连矿池 TLS 握手可能十几秒。如果你有一台离矿池近的 VPS，可以中转：

```json
{
  "tunnel_host": "user@你的VPS",
  "tunnel_local_port": 18443,
  "pool_tls": "global.pearlfortune.org:443"
}
```

矿机会跑 `ssh -N -L 127.0.0.1:18443:矿池:443 user@VPS`，矿工连本地端口。

**强烈建议用受限密钥**，在 VPS 的 `~/.ssh/authorized_keys` 里：

```
restrict,port-forwarding ssh-ed25519 AAAA... your-key
```

这样密钥泄露也只能做端口转发，不能登 shell。

不想用隧道就留空，矿机直连。

### 4.9 开机自启（F11，可选）

**Windows**：`Win+R` → `shell:startup` → 把 `guardian.py` 的快捷方式放进去，
目标写 `pythonw.exe guardian.py`（`pythonw` 不弹黑窗）。

守护进程有**单实例保护**，自启和手动启动同时触发也不会双开。

**Linux**（看板服务）：

```ini
# /etc/systemd/system/pearl-dashboard.service
[Unit]
Description=Pearl Dashboard
After=network.target

[Service]
WorkingDirectory=/opt/pearl-self-dashboard/dashboard
ExecStart=/usr/bin/python3 /opt/pearl-self-dashboard/dashboard/server.py 8088
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now pearl-dashboard
```

### 4.10 域名 + HTTPS（F12，可选）

Caddy 反代示例：

```caddyfile
your-domain.example.com {
    handle /prl/* {
        uri strip_prefix /prl
        reverse_proxy 127.0.0.1:8088
    }
}
```

访问 `https://your-domain.example.com/prl/`。Caddy 会自动签证书。

### 4.11 公开只读页（F13，可选）

看板页面本身没有控制按钮时即为只读。需要提供一个"只能看、不能操作"的页面：

- 直接把看板地址给他（如果没开 F7，他只能看）
- 想更彻底：复制 `dashboard/static/index.html` 成 `public.html`，删掉 `<div class="ctl">…</div>` 那一块，
  再用反代单独映射一个路径

### 4.12 Bark 手机推送（F14，可选）

用 [Bark](https://github.com/Finb/Bark)（iOS 免费 App）把状态推到手机。
**推送跑在服务器上**，所以矿机掉线时也能收到提醒——矿机自己是发不出"我掉线了"的。

**第一步：拿到你的 key**

1. App Store 装 Bark
2. 打开 App，首页那串 `https://api.day.app/xxxxxxxxxxxx/` 里的 `xxxxxxxxxxxx` 就是 key

**第二步：填到服务器**

```bash
mkdir -p pearl-mining/secrets
echo '你的key' > pearl-mining/secrets/bark.key
chmod 600 pearl-mining/secrets/bark.key
```

**第三步：重启看板服务**

```bash
sudo systemctl restart pearl-dashboard
```

重启后会立刻发一条汇报，用来验证配置对不对。

**你会收到四类消息**

| 类型 | 触发 | 频率 |
|---|---|---|
| 定期汇报 | 每 30 分钟 | 固定 |
| 🌡️ 高温 | 温度 ≥ 75°C | 每次触发后 30 分钟内不重复 |
| ⚠️ 掉线 / ✅ 恢复 | 上报超过 150 秒没更新 | 状态变化时 |
| ⏸️ 暂停 / ▶️ 恢复 | 手动 / 高温保护 / 游戏暂停 | 状态变化时 |

汇报长这样：

```
PRL 数据  160.2 TH/s · 2/2 在线

rig-a: 79.9T----56°C----125W----进行中
rig-b: 80.3T----66°C----134W----进行中
过去1h 0.0628  过去6h 0.2215  过去24h 0.2215 PRL
```

**可调参数**（`dashboard/server.py` 顶部）

```python
SUMMARY_SEC  = 1800    # 汇报间隔（秒）
TEMP_WARN    = 75.0    # 温度告警线
TEMP_REALERT = 1800    # 高温持续时再次提醒的间隔
OFFLINE_AFTER = 150    # 多久没上报算掉线
```

> 没有填 `secrets/bark.key` 时，所有推送会静默跳过，不影响其他功能。
> 这个文件已在 `.gitignore` 里，不会误提交。

---

### 活动记录（挖矿 / 暂停 / 关机）

看板最下面的「活动情况」是从 30 秒采样历史反推的，判断规则：

| 状态 | 判断 | 含义 |
|---|---|---|
| 挖矿中 | 算力 > 0 | 正常 |
| 暂停 | 算力 = 0，但功耗/温度还有值 | 机器开着，只是没挖（打游戏 / 手动停 / 高温保护） |
| 关机 | 算力、功耗、温度全为 0 | 关机或断网 |

- 不足 **10 分钟**的片段不记录
- 每页 10 条，可翻页
- 进行中的状态显示「现在 / 进行中」

> 关键点：**服务器判断一台机器是否"在挖"，看的是上报是否新鲜**（超过 150 秒没上报即视为离线）。
> 否则机器关机后，最后一次上报的算力会被一直沿用，账本和曲线都会虚高。

## 5. 配置参考

### `agent/config.json`

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `worker` | string | `rig-a` | 机器名，全局唯一 |
| `wallet` | string | — | PRL 收款地址 |
| `url` | string | — | 上报地址，如 `http://host:8088/api/rig` |
| `dashboard_url` | string | `http://127.0.0.1:8088` | 桌面控制台打开看板用 |
| `token` | string | — | 上报令牌（服务端 `data/.rig_token`） |
| `interval_sec` | int | `10` | 上报间隔 |
| `miner_log` | string | `official.log.err` | 矿工日志路径 |
| `tunnel_host` | string | `""` | SSH 中转，空=直连 |
| `tunnel_local_port` | int | `18443` | 本地转发端口 |
| `pool_tls` | string | `global.pearlfortune.org:443` | 矿池 TLS 地址 |

### `thermal.json`

见 [4.6](#46-温度保护f5可选)。

### `games.txt`

见 [4.7](#47-游戏自动暂停f6可选)。

### `dashboard/server.py` 顶部常量

```python
POOL_FEE  = 0.05    # 矿池手续费
MINER_FEE = 0.01    # 矿工费
ELEC_KW   = 0.220   # 整机功率（kW）
ELEC_CNY  = 0.50    # 电价（元/度）
USD_CNY   = 6.7     # 汇率
```

改完重启服务生效。

---

## 6. HTTP API

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/rig` | `X-Rig-Token` | 机器上报 |
| GET | `/api/rig?w=rig-a` | 无 | 单机最新状态 + 收益 |
| GET | `/api/earnings?w=rig-a` | 无 | 产出：`last24_prl`、`accrual[]`、`payouts[]`、`price_usd` |
| GET | `/api/history?w=rig-a&h=6` | 无 | 算力曲线（0.1~168 小时） |
| GET | `/api/events?w=rig-a&limit=20` | 无 | 在线/离线事件（≥3 分钟才记） |
| GET | `/api/workers` | 无 | 机器清单 |
| GET | `/api/net` | 无 | 全网难度 / 币价 |
| **GET** | **`/api/cmd?w=rig-a`** | **无（只读）** | **该机器待执行的命令** |
| **POST** | **`/api/cmd`** | **`X-Ctl-Key`** | **下发暂停/恢复** |

上报示例：

```bash
curl -X POST http://127.0.0.1:8088/api/rig \
  -H "X-Rig-Token: <token>" -H "Content-Type: application/json" \
  -d '{"worker":"rig-a","total_th":84.5,"power_w":124,"max_temp":56,"state":"mining"}'
```

---

## 7. 数据口径

**过去 24 小时产出**用算力历史积分反推，不是"从零点累计"：

```
每 TH 每秒产出 = (1/出块时间) × 块奖励 × (1e12 / (全网难度 × 1e18)) × (1-池费) × (1-矿工费)
```

按 30 秒一条的算力历史在最近 24 小时做梯形积分 —— 好处是**上线当天就有数**。

**账本口径**：每台设备各自维护独立账本，累计产出为该账本的历史累加。
折合人民币 = 当前币价 × 汇率。

> 日净利是**预估**：实际收益受运行时间、功率稳定性影响，打游戏/高温/断线时都会低于预估。

---

## 8. 常见问题

**Q：看板上没有我的机器？**
检查 `agent/config.json` 的 `token` 与服务器 `data/.rig_token` 是否一致；再看 `guardian.log`。

**Q：算力一直是 0？**
矿工启动后需要 1–2 分钟才输出算力；确认 `miner_log` 路径正确且文件在更新。

**Q：为什么 5060 Ti 和 5060 算力差不多？**
PearlHash 吃显存带宽，这两张卡带宽完全相同（448 GB/s），核心多发挥不出来。这是算法特性。

**Q：Windows 能超频吗？**
官方矿工自带的超频参数在 Windows 会直接报错，要用显卡厂商工具（Afterburner / NVIDIA App）在驱动层设置。

**Q：温度多少危险？**
83℃ 开始降频，95℃ 以上触发保护。7×24 建议压在 75℃ 以下。

更多见 [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)。

---

## 9. 安全说明

- **控制密码**：服务端首次启动自动生成 `secrets/ctl.key`（32 位随机）。想换就删掉它重启
- **上报令牌**：`data/.rig_token`，防止别人伪造你的数据
- **本仓库不含任何密码、地址、IP**，全部在本地配置里
- **矿机不开入站端口**，只往外发请求
- SSH 隧道请用 `restrict,port-forwarding` 受限密钥
- `agent/config.json`、`secrets/`、`data/` 都已加进 `.gitignore`，不会误提交

---

## 10. 许可

[MIT License](LICENSE)

矿池：[Pearl Fortune](https://pearlfortune.org)（PPS 5%） · 链上：[PearlTrack](https://pearltrack.io) / [prlscan](https://prlscan.com)
