# HTTPS 上传与网页图库（设计 · 实现 · 部署 · 运维）

> 照片不用 scp，改用 HTTPS（WebDAV）上传到 manjucc，并且上传后能从 PC 或手机的浏览器查看。
>
> 本文覆盖四部分：设计思想、实现（capture.py 端）、服务器端部署（manjucc 端）、日常使用与运维。文中 V4/V5 是开发阶段的内部代号（V4 = 早期的 scp/ssh 方案，V5 = 现行的 HTTPS 方案），对照见 3.5 节。

---

## 1. 需求与总体形态

方案的五点要求与落地方式：

| 要求 | 落地 |
|---|---|
| 1. 用 HTTPS 上传，不用 scp | WebDAV：`PUT` 到隐藏 `.part` 名 + `MOVE` 到正式名（nginx 内置 dav 模块） |
| 2. manjucc 已装 nginx 且跑着网站 | 不动主站内容，只在原配置上加 443 与跳转 |
| 3. manjucc.com 改用 HTTPS，自签名证书 | 一张自签名证书同时覆盖 `manjucc.com`、`www`、`capture.manjucc.com` 和 ECS IP；`:80` 301 跳转到 `https://` |
| 4. 上传目录自斟酌 | `/var/www/captures`（nginx 可直接读写，浏览器可直接访问） |
| 5. 浏览地址 `https://capture.manjucc.com/` | 独立 vhost，autoindex 图库；另设等效只读入口 `https://manjucc.com/captures/` |

```
Raspberry Pi 5                            manjucc（阿里云 ECS，118.178.229.25，Ubuntu 24.04）
┌──────────────────────┐    HTTPS/TLS     ┌────────────────────────────────────────────┐
│ capture.py           │   （证书锁定）     │ nginx 1.24（发行版自带，零额外组件）           │
│  ├ OpenCV/V4L2 拍摄   │═════════════════▶│  ├ PUT / MOVE  (dav 模块，写入)  ─┐         │
│  ├ YOLOv5 人形检测     │                  │  ├ autoindex   （图库，读取）     ├▶ /var/www/captures
│  └ HttpsUploader     │                  │  └ Basic Auth  （读写都要登录）    │   *.jpg
│     凭据: ~/.config/  │                  │                                   │   capture-list.txt
│     capturer/upload.json                │  manjucc.com:80  ──301──▶ https://manjucc.com/（主站不变）
└──────────────────────┘                  │  https://capture.manjucc.com/（图库/上传入口）
                                          └────────────────────────────────────────────┘
      PC / 手机浏览器 ──────────────────────────────┘（两个图库 URL 均可，需登录）
```

---

## 2. 设计思想

### 2.1 为什么是 WebDAV（PUT + MOVE），而不是别的

- **不写服务端代码**：nginx 的 `--with-http_dav_module` 是 Ubuntu nginx 自带编译的，`PUT`/`MOVE` 开箱即用——服务器上不用装任何包、不用跑任何额外的进程/语言运行时，攻击面和维护量最小。
- **延续 V4 的原子语义**：V4 用 "scp 到 `.名字.part`，再 ssh `mv` 到正式名" 保证半截传输永远不会以正式名字出现。V5 完全等价：先 `PUT` 到 `.{名字}.part`，成功后再一条 `MOVE`（带 `Destination` 与 `Overwrite: T`）改名为正式名。nginx 的 dav 实现本身也是"临时文件 + rename"，双保险。
- **异步与放弃语义照旧**：上传在守护线程里做，主拍摄循环只投队列、永不阻塞；每张照片有总时间预算，超时即放弃，网络坏了不影响拍摄、不涨内存。

### 2.2 安全模型

- **TLS 锁定而不是关掉校验**：服务器证书是自签名的，`HttpsUploader` 不用 `verify=False`，而是把服务器证书本身（`upload.json` 里的 `ca` 字段）作为信任锚传给 requests——等于 SSH 的 TOFU（first use trust on sight）：第一次部署时认定这张证书，之后任何人中途换证书/中间人都会握手失败。
- **上传要认证**：PUT/MOVE 走 HTTP Basic Auth（否则公网上谁都能往你服务器写文件）。
- **浏览也要认证**：照片里是"检测到的人"，服务器在公网——默认把图库也放在 Basic Auth 后面（用户 `Capturer`；apr1 哈希不含用户名，将来改名只需重写 htpasswd 一行）。若想公开，删掉 capture vhost `location /` 里的两行 `auth_basic*` 即可。
- 证书 **395 天**有效期：Chrome/Safari/Firefox 对新签发证书强制 ≤398 天，签 10 年会被浏览器直接拒绝（curl 不受影响，但手机浏览器是硬需求）。

### 2.3 capture-list.txt 的维护策略

> 名单文件在 scp 方案时期叫 `captured.list`，现名 `capture-list.txt`；服务器与 Pi 本地镜像已同步改名，历史条目原样保留。

V4 可以 ssh 上去 `>>` 追加一行；HTTP/nginx 是无状态的，没有"append"操作，所以 V5 改为：

- 每次上传成功后，把文件名**追加到本地镜像**（输出目录下的 `capture-list.txt`）；
- **整文件重传**到服务器（覆盖同名文件）：距上次推送 ≥30 s 且累计 ≥8 个新名字才推（或距上次 ≥5 分钟，先到为准），程序退出时再冲刷一次；
- 整文件覆盖有风险：如果启动时服务器上的旧列表拉不下来（网络断、5xx），而本地镜像是空的，第一次重传就会把服务器历史冲掉。因此加了**同步门槛**：镜像必须先与服务器副本同步过才允许推送；拉不下来就只记录不推送，等恢复后**合并**（服务器行在前、本地新增在后，都是时间序，天然有序）再推。服务器返回 404 视为"还没有列表"，允许从空起步。

### 2.4 目录选在哪

`/home/andy/captures` 的问题：家目录权限通常 750，nginx（www-data）读不进去，还得开家目录权限（副作用大）。`/var/www/captures` 是 nginx 的地盘：目录属主 `www-data:www-data` 755——nginx 既能读（图库）也能写（dav 上传），andy 管理时用 sudo。旧目录 `/home/andy/captures` 原样保留为 V4 存档（其中的 `captured.list` 是冻结的历史副本），236 张旧图和名单已迁入新目录（今名 `capture-list.txt`）。

---

## 3. 实现（capture.py 端）

### 3.1 组件与数据流

`HttpsUploader`（替代 V4 的 `Uploader`，接口不变：`start()` / `submit()` / `stop()`）：

```
capture 线程                        uploader 线程                          nginx (manjucc)
    │  submit(path) ──队列(上限128)──▶ │
    │   （满了就丢，不阻塞）            │ PUT /.{name}.part   照片字节, Basic Auth
    │                                │──────────────────────────────────▶  dav 写入
    │                                │◀── 2xx ───────────────────────────
    │                                │ MOVE /.{name}.part
    │                                │   Destination: /{name}, Overwrite: T
    │                                │──────────────────────────────────▶  改名为正式名
    │                                │◀── 2xx ───────────────────────────  ← 此刻起图库可见
    │                                │ 本地镜像 append name
    │                                │ （新增 ≥8 条且距上次 ≥30s，或距上次 ≥5 分钟，或退出时）PUT /capture-list.txt 整文件
```

### 3.2 超时与重试的三个层级

| 层级 | 数值 | 作用 |
|---|---|---|
| 单个 HTTP 请求 | 连接 5 s / 读 5 s（`UPLOAD_CALL_TIMEOUT`） | 对端僵死时快速失败进入重试 |
| 每张照片总预算 | `--upload-timeout`，默认 30 s | 到期放弃该照片（"超时就不传了"），计 `failed` |
| 重试间隔 | 1 s 退避，按 0.25 s 切片感知停止 | 停机请求能及时打断重试循环 |

停止路径：`stop()` 在一个 `UPLOADER_STOP_BUDGET`（7 s）的总预算内分两段 `join`（先 ≤2 s 让健康的在传照片收尾，再用剩余预算等卡住的调用按其读超时返回）；预算整体落在主线程看门狗的 `STOP_GRACE`（10 s）之内，所以最终的 `capture-list.txt` flush 不会被 `os._exit` 截断——Ctrl+C 到退出最多 ~10 s，正常秒级。

### 3.3 配置：`~/.config/capturer/upload.json`（权限 600）

```json
{
  "url": "https://capture.manjucc.com",
  "user": "Capturer",
  "password": "……",
  "ca": "/home/pi/.config/capturer/manjucc-capture.crt"
}
```

- `user`/`password` 必填；`ca` 给出则作为 TLS 信任锚（证书锁定）；`url` 可被命令行 `--remote-url` 覆盖。
- 校验（`load_upload_config`）：必须是 `http(s)://host[:port][/前缀]` 形式；`ca` 指向的文件必须存在。配置缺失/损坏 = 启动错误（退出码 1），不是静默关掉上传。
- 命令行相应变化：`--remote-host/--remote-dir` 删除，新增 `--remote-url`、`--upload-config`；`--upload-timeout`、`--no-upload` 保留。上传仍只在检测模式发生（`--no-detect` = 不检测也不上传）。

### 3.4 若干实现细节（为什么这么写）

- `session.trust_env = False`：忽略环境变量代理，避免代理介入对自签名证书做手脚。
- `allow_redirects = False`：301/302 会把 PUT 悄悄降级成 GET（requests 的重定向语义），宁可显式失败。
- 文件名经 `urllib.parse.quote` 编码后拼 URL，`Destination` 用绝对 URL（WebDAV 规范要求）。
- `capture-list.txt` 重传是幂等的整文件覆盖，撕包（半截 PUT）会在下一次推送或下次运行种子时自愈。
- 本地镜像缺失时的种子：GET 服务器列表，200 → 全量种子；404 → 空起步；其他 → 暂不推送（见 2.3）。

### 3.5 scp 方案与 HTTPS 方案对照

| | scp 方案（V4，旧） | HTTPS 方案（V5，现行） |
|---|---|---|
| 传输 | scp + ssh（子进程） | HTTPS WebDAV（requests，PUT+MOVE） |
| 目标 | `manjucc:/home/andy/captures` | `https://capture.manjucc.com` → `/var/www/captures` |
| 认证 | ssh 公钥 | Basic Auth + TLS 证书锁定 |
| 原子性 | scp `.part` + ssh `mv` | PUT `.part` + MOVE |
| 列表维护 | ssh 追加一行 | 本地镜像 + 整文件限频重传（同步/合并保护） |
| 超时中止 | 0.5 s 切片 kill 子进程 | 单请求 5 s + 每照片 30 s 总预算 + 停止感知重试 |
| 前置动作 | 启动时 mkdir -p 远端目录 | 无需（固定 root） |
| 可浏览 | 否 | 是（图库） |

### 3.6 测试

`tests/local_upload_check.py`：内置一个迷你 HTTPS+WebDAV 存根服务器（自签名证书、Basic Auth、PUT/MOVE/GET/autoindex、可编程故障注入），对**真实的 `HttpsUploader`** 做 26 项断言——三张照片原子落地、无 `.part` 残留、列表内容与顺序、重启后从服务器重新种子、第四张正确续接、密码错误在超时内放弃且服务器无残留、列表"拉不到就不覆盖 + 恢复后合并"、PUT/MOVE 各 500 一次后经重试梯子自愈（残留 `.part` 被复用而非泄漏）、404 空起步、301 不被跟随、错误锁定 CA 握手失败、队列满丢弃与停机放弃的计数口径。不碰网络，随时可跑：

```bash
python3 -I tests/local_upload_check.py
```

`tests/test_capture_logic.py`：无摄像头/无 torch/无网络的纯逻辑单测（10 项断言）——`normalize_device`（`-d 0` 裸索引）、`free_gib` 与 `shutil.disk_usage` 对拍、`parse_args` 新参数校验、同秒文件名 `-2/-3` 后缀、`purge_old_captures` 只删顶层且只删本项目命名的旧文件：

```bash
python3 -I tests/test_capture_logic.py
```

---

## 4. 服务器端部署（manjucc）

### 4.1 部署了什么

| 路径 | 内容 |
|---|---|
| `/etc/nginx/ssl/manjucc.crt` + `.key`(600) | 自签名证书：CN=manjucc.com；SAN = manjucc.com, www.manjucc.com, capture.manjucc.com, IP:118.178.229.25；RSA 2048 / SHA-256 / **395 天**；带 `CA:TRUE` 与 `serverAuth`，可自任信任锚 |
| `/etc/nginx/htpasswd-capture` | Basic Auth 凭据（用户 `Capturer`，apr1 哈希，与 Pi 端 `upload.json` 对应） |
| `/etc/nginx/sites-available/default` | 改写：`:80`（default_server）只做 301 → https；`:443`（default_server）服务原主站 `/var/www/html`；附只读别名 `location /captures/` |
| `/etc/nginx/sites-available/capture.manjucc.com` | 新增：`:80` 301；`:443` root=`/var/www/captures`，autoindex，`dav_methods PUT MOVE`，`client_max_body_size 10m`，`location /` 内 Basic Auth |
| `/var/www/captures` | www-data:www-data 755；V4 旧图 + 名单（`capture-list.txt`）已从 `/home/andy/captures` 迁入（旧目录保留；测试期照片已清理，目录由上传流重新填充） |
| `/etc/systemd/system/capture-retention.{service,timer}` | 90 天照片保留：每日 03:30 以 www-data 身份 `find` 删除超过 90 天的 `*.jpg`（**capture-list.txt 永不删**，见 4.5） |
| `~/nginx-config-backup-*.tar.gz`、`/etc/nginx/sites-available/default.pre-v5` | 部署前完整备份（回滚用；`default.pre-v5` 与首个 tar 只在首次部署时生成，重跑不覆盖，tar 名精确到分钟） |

关键一点：**PUT/MOVE 与浏览共用同一个 `location /` 的 Basic Auth**，读写都需登录；上传期间短暂存在的 `.part` 文件会出现在 autoindex 里（点开头、秒级存在、也要登录才能看），纯观感问题。

### 4.2 一键（重）部署与证书续期

`server/deploy-v5.sh` 幂等，可反复跑（备份→证书→建目录并迁移→装配置→`nginx -t`+reload→保留 timer→冒烟测试）：

```bash
scp server/deploy-v5.sh manjucc:/tmp/
ssh manjucc 'SUDO_PASS=<andy的sudo密码> CAP_PASS=<capture密码> bash /tmp/deploy-v5.sh'
```

证书处理：脚本先用 `openssl x509 -checkend 30d` 检查现有证书——**剩余 >30 天就跳过重签**（Pi 端锁定的 CA 不受影响，重跑安全）；到期的 30 天内重跑才会重签，此时脚本会醒目打印 `RE-PIN THE PI`，且**必须**把新证书同步回 Pi，否则上传会因证书校验失败而全部放弃（这是设计使然的失败，日志会明说）：

```bash
# 巡检：还剩多久到期（当前这张 2027-12-11 到期）
ssh manjucc 'openssl x509 -in /etc/nginx/ssl/manjucc.crt -noout -enddate'
# 确实重签过之后：
scp manjucc:/etc/nginx/ssl/manjucc.crt ~/.config/capturer/manjucc-capture.crt
```

### 4.3 网络前提（都曾是实际踩过的坑）

- **阿里云安全组必须放行入方向 TCP 443**。部署时曾默认只开了 80：现象是外部 443 连接超时、服务器本机 `ss` 明明在听、ufw/iptables 全开。排查口诀：`ss` 在听 + 服务器防火墙开放 + 外部超时 ⇒ 安全组。
- **`capture.manjucc.com` 需要 DNS A 记录 → 118.178.229.25**（已由用户添加）。NXDOMAIN 期间靠两件事顶着：Pi 的 `/etc/hosts` 里加了 `118.178.229.25 capture.manjucc.com`（如今作无害兜底保留，**换 ECS IP 时记得同步**），以及 `https://manjucc.com/captures/` 这个随时可用的等效入口。

### 4.4 部署中的两个事故与教训（已修复、已写进脚本注释）

1. `printf 密码 | sudo -S tee 文件`：sudo 会把 stdin 的密码行吃掉，`tee` 拿到的是空流，**写出 0 字节配置**——nginx reload 后全站下线。教训：root 文件一律"mktemp 暂存 + `sudo install`"（脚本已改）。所幸有 `nginx -t` 与事前备份，一两分钟内恢复。
2. 列表被冲掉：上述空配置期间一次"空镜像成功上传"触发了整文件重传，把服务器 `captured.list` 覆盖成一行。从 `/home/andy/captures` 恢复，并催生了 2.3 节的同步/合并保护——现在是代码保证，不靠运气。

### 4.5 磁盘与保留策略（两端）

两端的照片都不会无限增长：

- **服务器**：`capture-retention.timer` 每日 03:30 触发 `capture-retention.service`，以 www-data 身份删除 `/var/www/captures` 下超过 90 天的 `*.jpg`（只删顶层、只删 `.jpg`）。**`capture-list.txt` 永不清理**——它是"每张上传过的照片"的历史记录，而且 Pi 端的合并语义（服务器列表优先、本地新增并入）会把被裁掉的行在下一次整文件 PUT 时原样推回去，裁它只会丢历史。手工巡检：
  ```bash
  ssh manjucc 'systemctl list-timers --no-pager capture-retention.timer; du -sh /var/www/captures'
  ```
- **Pi（`--retain-days N`，默认 0 = 不清理）**：启动时与磁盘触底时，`purge_old_captures` 删除输出目录（默认 `/home/pi/TF`）里**只匹配本项目命名** `YYYY-MM-DD_HHMMSS[-N].jpg` 且 mtime 超过 N 天的顶层文件——目录里用户自己的文件（`capture.py`、`capture-list.txt`、`yolov5-USB/` 等）永不被碰，子目录不下探。低磁盘退出（退出码 2）也变成"先清理自救、复查仍不足才退出"：循环内与启动预检两处都是这个顺序。

---

## 5. 如何使用

### 5.1 运行（Pi 上）

```bash
python3 capture.py          # 全流程：5s 拍摄（默认间隔）→ 人形检测（内部推理 480，实测 ~0.65s/帧）→ 存盘（JPEG 质量 85，~17 KB/张）→ HTTPS 上传 → 列表维护
                            # 前台运行；Ctrl+C 停止；168 h 自动退出；磁盘 <2GiB 退出码 2
```

常用参数：`--no-detect`（不检测也不上传）、`--no-upload`（检测+存盘但不上传）、`--remote-url`（临时换目标）、`--upload-config`（换凭据文件）、`--upload-timeout`、`-c/--conf`、`-o/--output-dir`、`--min-free-gib`、`--infer-size`（检测输入长边，默认 480；只影响检测速度与远距召回，照片仍 640x480 全分辨率保存，实测 640→480 单帧 1.04s→0.64s）、`--retain-days`（本地保留天数，默认 0 不清理，见 4.5）……完整见 `python3 capture.py --help`。

日志即 stdout（INFO 级）：每张上传成功有 `uploaded <名字> to <URL>`，放弃有 `not done within Ns; abandoned`，列表问题有 `capture-list.txt updates stay off …` 一类明确说法。

### 5.2 浏览（PC / 手机）

| 入口 | 说明 |
|---|---|
| `https://capture.manjucc.com/` | 规范地址，DNS 已生效 |
| `https://manjucc.com/captures/` | 等效只读入口（永远可用，可作收藏夹） |

- 登录：用户 `Capturer`，密码见 Pi 上 `~/.config/capturer/upload.json` 的 `password` 字段；浏览器记住后不再问。
- 自签名证书：首次访问会警告一次，各设备接受一次即可（手机上"高级 → 继续访问"）。
- 页面是 nginx autoindex 文件列表，点击文件名看图；按名字即时间排序，最新在最后。

### 5.3 日常检查与运维

```bash
# Pi 上：离线自测（26 项断言，不碰网络）+ 纯逻辑单测（10 项）
python3 -I tests/local_upload_check.py
python3 -I tests/test_capture_logic.py

# Pi 上：命令行看一眼图库（与上传同一张锁定证书 + 凭据）
PW=$(python3 -c "import json; print(json.load(open('/home/pi/.config/capturer/upload.json'))['password'])")
curl --cacert ~/.config/capturer/manjucc-capture.crt -u "Capturer:$PW" https://capture.manjucc.com/ | tail

# 服务器上：看列表与文件
ssh manjucc 'tail /var/www/captures/capture-list.txt; ls /var/www/captures | tail'

# 服务器上：删图/清理（目录属 www-data，需 sudo）
ssh manjucc 'sudo rm /var/www/captures/<某张>.jpg'

# 换 Capturer 密码（两端都要改）
#   服务器：printf 'Capturer:%s\n' "$(openssl passwd -apr1 '新密码')" | sudo tee /etc/nginx/htpasswd-capture
#   Pi：     改 ~/.config/capturer/upload.json 的 "password"，重启 capture.py
```

### 5.4 故障排查速查

| 现象 | 首先查 |
|---|---|
| 上传全部 `abandoned`（连接超时） | 阿里云安全组 443（发生过）；Pi 的 DNS / `/etc/hosts` 里 capture.manjucc.com 指向 |
| 日志 `HTTP 401` | `upload.json` 密码与服务器 `htpasswd-capture` 不一致 |
| 日志 TLS/证书错误（`SSLError`） | 服务器重签过证书而 Pi 没同步 → `scp manjucc:/etc/nginx/ssl/manjucc.crt ~/.config/capturer/manjucc-capture.crt`（2027-12 前后还要想到**证书自然到期**这一种：先 `openssl x509 -enddate` 确认，到期就重跑 deploy-v5.sh 触发重签，再 scp 回 Pi） |
| 日志 `capture-list.txt updates stay off` | 服务器列表暂时拉不到——不推送是保护行为，恢复后自动合并；照片本身不受影响 |
| 浏览器打不开 443 站点 | 证书警告属预期（自签名）；连不上则同第一行查安全组/DNS |
| 图库 404 | URL 少了结尾 `/`（目录地址要斜杠）；或文件名敲错 |

### 5.5 已知限制

- autoindex 会在上传的瞬间列出 `.part` 文件（秒级、需登录，观感问题）。
- `capture-list.txt` 是整文件重传：列表越长每次推送越大——本机规模（每天千行级，约几十 KB）完全无感，若做成长期归档可再改为分文件/分月列表。
- 单写者假设：同一时刻只应有一个 capture.py 实例往同一 URL 上传（本机即如此）。
- 上传仅在检测模式触发：`--no-detect` 不上传。
