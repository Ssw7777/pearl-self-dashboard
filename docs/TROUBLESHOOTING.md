# 故障排查

按「现象 → 原因 → 处理」组织。先看日志：`guardian.log`（守护）、`official.log.err`（矿工）。

---

## 1. 看板上没有这台机器

**检查顺序**

1. `guardian.log` 里有没有 `agent: starting`
2. `agent/config.json` 里 `dashboard_url` 能不能打开
3. `token` 是否与服务端 `data/.rig_token` 一致
4. 服务端日志有没有 `401 bad token`

**手动测试上报是否通**

```powershell
curl -X POST http://你的服务器:8088/api/rig `
  -H "X-Rig-Token: 你的令牌" -H "Content-Type: application/json" `
  -d '{\"worker\":\"rig-a\",\"total_th\":1}'
```

返回 `{"ok":true}` 说明链路通，问题在本机上报端。

---

## 2. 算力一直是 0

- 矿工刚启动需要 **1–2 分钟**才会输出算力
- `miner_log` 指向的文件是否在更新（看修改时间）
- 矿工是否真的在跑：`tasklist | findstr miner`
- 日志里搜 `proof_per_sec`，有就说明能解析

---

## 3. 矿工起不来 / 闪退

**杀毒软件误报（最常见）**

Windows Defender、火绒等会把挖矿程序判定为 `HackTool/CoinMiner`。
需要把项目目录加入信任区：

- Defender：设置 → 病毒和威胁防护 → 排除项 → 添加文件夹
- 火绒：病毒查杀 → 信任区 → 添加目录

**矿工参数不支持**

Windows 下矿工**不支持任何超频参数**（`--gpu-coffset`、`--gpu-fan`、`--gpu-plimit` 等），
带上会直接报：

```
overclock settings are not supported on Windows
```

去掉这些参数即可。

---

## 4. 温度保护频繁触发

- 看 `guardian.log` 里的 `thermal` 记录，确认是不是真到了阈值
- 机箱风道、灰尘、环境温度是主因
- 阈值可以调：`thermal.json` 里 `hot` 提到 82、`cool` 保持 70

---

## 5. 隧道连不上（如果启用了 `tunnel_host`）

```
ssh: connect to host ... port 22: Connection refused
```

- 服务器 SSH 是否在跑
- 密钥权限：Windows 下私钥文件不要给别人读
- 受限密钥写法（推荐）：

```
restrict,port-forwarding ssh-ed25519 AAAA... your-key
```

- 本地端口被占用：`netstat -ano | findstr 18443`

**不想用隧道**：把 `tunnel_host` 留空，矿机会直连矿池（延迟高一点但能挖）。

---

## 6. 后台不停弹黑窗口

所有 `subprocess` 调用必须带 `creationflags=0x08000000`（`CREATE_NO_WINDOW`），
否则每次 `tasklist`/`nvidia-smi` 都会闪一下控制台。本项目已处理，若你改动代码，注意保留。

---

## 7. 页面中文乱码 / 文件被写坏

**不要**用 `Set-Content -Encoding UTF8` 或 `Out-File` 改这些文件（PowerShell 5.1 会写成 GBK/带 BOM）。
用 Python 或 .NET：

```powershell
[IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false)))
```

另外：`.sh` 脚本必须是 **LF** 换行，Windows 下写的 CRLF 会让 bash 报
`set: pipefail: invalid option name`。

---

## 8. 打款记录一直为空

- 打款记录读的是**链上**转入交易，不是池子内部账本
- 池子要在「区块成熟 + 达到打款门槛」之后才会真正转账
- 检查地址是否配置正确（`data/workers.json` 里的 `address` 字段）

---

## 9. 桌面控制台尺寸不对 / 文字被裁

`panel.py` 里 `W_WIN, H_WIN = 900, 500` 是窗口尺寸，
字体大小在 `_card()` / `_info_cell()` 里，调小一点就能塞下更多内容。

---

## 10. 服务端 502 / 打不开

- `systemctl status pearl-dashboard` 是否 active
- 端口是否被占：`ss -lntp | grep 8088`
- 反代是否配对：Caddy 里 `uri strip_prefix` 去掉前缀后，服务端收到的是 `/api/...`

> 注意：`serve.sh` 这类脚本如果是 Windows 上编辑后再上传，务必确认是 LF 换行，
> 否则 bash 会崩在第二行。

---

## 11. 关掉某个程序后挖矿自动停了

**现象**：从某个 IDE / 终端 / Electron 应用里启动守护，关掉它以后矿机跟着停了。

**原因**：Windows 有「作业对象（Job Object）」。宿主程序创建一个带
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的作业，把自己和所有子进程放进去。
宿主一退出，作业关闭 → **整棵子树被系统杀掉**。这不是挖矿程序的问题，任何子进程都一样。

**判断方法**：看进程的父进程是谁。

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Select-Object ProcessId, ParentProcessId, Name
```

如果父进程是那个宿主程序（或其终端），就中招了。

**三种脱离办法（按推荐顺序）**

1. **开机自启快捷方式**（最简单）
   把 `pythonw.exe guardian.py` 的快捷方式放进 `shell:startup`，
   登录时由 `explorer.exe` 拉起，天然在宿主作业之外。

2. **WMI 启动**（无需管理员）
   ```powershell
   Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
     -Arguments @{ CommandLine = '"C:\Python\pythonw.exe" "C:\prl\guardian.py"' }
   ```
   父进程会变成 `WmiPrvSE.exe`（系统服务），和任何用户程序都无关。

3. **计划任务**（需要管理员）
   ```powershell
   schtasks /Create /TN "PRL Guardian" /TR '"...\pythonw.exe" "...\guardian.py"' /SC ONLOGON /F
   ```

**验证**：启动后父进程应该是 `explorer.exe` / `WmiPrvSE.exe` / `taskhostw.exe`，
而不是你的终端或 IDE。
