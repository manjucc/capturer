# Capturer

[English](README.md) | **简体中文**

树莓派 5 上的定时摄像头拍摄 + 人形检测程序，配合远程服务器上可浏览的
HTTPS 图库。

启动后，`capture.py` 每隔几秒从 USB 摄像头抓一帧，跑一遍 YOLOv5s 人形
检测——**只有画面里有人**时才把 JPEG 存到本地，并异步经 HTTPS 上传到
nginx 上的图库，PC / 手机浏览器都能看。程序前台运行，Ctrl+C 干净退出，
7 天自动终止，磁盘剩余空间低于 2 GiB 时也会提前退出。

## 功能

- **定时拍摄**：固定间隔从 V4L2 设备（默认 `/dev/video0`）取帧，全部经
  OpenCV 原生完成，不依赖外部采集工具
- **人形检测**：YOLOv5s 推理，画面里有人才存照片（`--no-detect` 则每张
  都存、不检测、不上传）
- **磁盘下限**：输出文件系统剩余空间低于 `--min-free-gib`（默认 2 GiB，
  `0` 关闭）即以退出码 2 退出；可先用 `--retain-days` 清理本程序产生的
  旧照片自救
- **异步 HTTPS 上传**：保留的照片经 WebDAV（`PUT` + `MOVE`、Basic Auth、
  锁定的自签名证书）上传——网络再差也不会卡住拍摄、不会涨内存；每张
  照片超过 `--upload-timeout` 即放弃
- **可浏览图库**：nginx autoindex 列出照片，旁边维护一份
  `capture-list.txt` 名单文件
- Ctrl+C / SIGTERM 干净停机、`--max-runtime` 自动终止、退出码有明确约定

## 环境要求

- 树莓派（在 Pi 5 / aarch64 上开发）+ Python 3.11
- USB 摄像头挂在 `/dev/video0`
- `v4l2-ctl`（可选，用于查看相机格式）
- 系统级安装的 Python 包：`torch`、`torchvision`、`Pillow`、`numpy`、
  `opencv-python`、`requests`
- 脚本旁一个名为 `yolov5-USB/` 的 YOLOv5 检出——**不在本仓库里**，
  见下一节

## 准备 yolov5-USB 文件夹

`capture.py` 通过 `torch.hub` 本地加载 YOLOv5：默认 `--weights` 指向脚本
旁边的 `yolov5-USB/yolov5s.pt`，并把这个 `.pt` 所在目录当作 `torch.hub`
仓库。该文件夹有意不入库（18 MB 的第三方代码加权重，上游随时可取），
克隆本仓库后自行准备一次即可：

```bash
# 1. 获取 yolov5 源码（v7.0 版 —— 本项目即基于它开发测试）
git clone --depth 1 -b v7.0 https://github.com/ultralytics/yolov5.git yolov5-USB

# 2. 下载 yolov5s 权重放进去
wget -O yolov5-USB/yolov5s.pt \
    https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5s.pt
```

没有 `git`？改用源码压缩包——
<https://github.com/ultralytics/yolov5/archive/refs/tags/v7.0.zip>——解压
后把得到的 `yolov5-7.0/` 改名为 `yolov5-USB/`，再按上面方式从 v7.0
release 下载 `yolov5s.pt` 放入。

愿意的话可以校验一下权重文件（本项目开发时用的就是这一份）：

```
大小:   14,808,437 字节
sha256: 8b3b748c1e592ddd8868022e8732fde20025197328490623cc16c6f24d0782ee
```

说明：

- 自定义 `-w/--weights` 可以指向任意 yolov5 检出**内部**的 `.pt`
  （其所在目录会成为 `torch.hub` 仓库）——那种情况下文件夹不必叫
  `yolov5-USB`。
- 开发与测试基于上游 **v7.0** 版；更新的版本一般也能用，但本项目未
  测试过。
- `.pt` 权重是 pickle 文件，加载时会执行代码——只用可信来源的权重。

## 快速上手

```bash
python3 capture.py
```

默认每 5 s 从 `/dev/video0` 拍一张，存到 `/home/pi/TF`（640x480），只在
有人时保存并上传到 `https://capture.manjucc.com`。Ctrl+C 停止；168 h
（7×24）后自动退出。

不检测、每张都存（从不上传）：

```bash
python3 capture.py --no-detect
```

检测 + 存盘但不上传：

```bash
python3 capture.py --no-upload
```

### 参数

| 参数 | 默认值 | 含义 |
|--------|---------|---------|
| `-o, --output-dir` | `/home/pi/TF` | 照片保存目录 |
| `-i, --interval` | `5` | 拍摄间隔（秒） |
| `-t, --max-runtime` | `604800`（168 h） | 运行这么多秒后自动退出 |
| `-d, --device` | `/dev/video0` | 采集用的 V4L2 设备 |
| `-r, --resolution` | `640x480` | 请求的分辨率 WxH（见下方说明） |
| `--no-detect` | 关 | 每张都存，不检测、不上传 |
| `-c, --conf` | `0.25` | 人形检测置信度阈值 |
| `--infer-size` | `480` | 检测输入的长边（更小更快；照片仍全分辨率保存） |
| `-w, --weights` | `yolov5-USB/yolov5s.pt` | YOLOv5 权重（默认相对脚本位置） |
| `--min-free-gib` | `2` | 剩余空间低于此 GiB 数即退出；`0` 关闭 |
| `--retain-days` | `0`（关） | 删除输出目录里超过 N 天的旧照片（只删本程序命名的文件）；低磁盘退出前先尝试清理 |
| `--remote-url` | 配置文件 / `https://capture.manjucc.com` | HTTPS 图库/上传基地址 |
| `--upload-config` | `~/.config/capturer/upload.json` | 上传凭据 + 锁定证书（见下） |
| `--upload-timeout` | `30` | 每张照片的上传时限，超时放弃 |
| `--no-upload` | 关 | 检测并存盘，但不上传 |
| `--version` | — | 打印版本号并退出 |

完整列表：`python3 capture.py --help`。

### 退出码

| 码 | 含义 |
|------|---------|
| 0 | 正常结束（Ctrl+C、SIGTERM 或 `--max-runtime` 到时） |
| 1 | 启动错误（相机/模型/配置问题，如上传配置缺失） |
| 2 | 剩余磁盘空间低于 `--min-free-gib` |
| 130 | 停机过程中收到第二个停止信号 |

## 检测是怎么工作的

`capture.py` 启动时加载一次 YOLOv5s（约 6 s），之后每帧在缩小的副本上
推理（`--infer-size`，默认长边 480：本机约 0.65 s/帧，640 时约 1.1 s），
照片按全分辨率保存（JPEG 质量 85，640x480 每张约 17 KB）。"人" 取 COCO
类目 0——YOLOv5 的 COCO 类目里没有"脸"，因此语义是"画面里有没有人"。
照片命名 `YYYY-MM-DD_HHMMSS.jpg`（同一秒内的后续帧追加 `-2`、`-3`……
后缀，快速连拍不会互相覆盖）。

**分辨率说明**：该摄像头只支持 1920x1080 / 1280x720 / 800x600 /
640x480——没有 640x360 模式。`capture.py` 直接请求 640x480；若相机实际
协商出别的尺寸会打一条警告。

## 上传

每张被保留的照片进入一个有界队列（128）交给守护线程，网络再差也不会
阻塞拍摄、不会涨内存。每张照片先 WebDAV `PUT` 到隐藏的
`.名字.jpg.part` URL，成功后一条 `MOVE` 改成正式名——半截传输永远不会
以正式名字出现。TLS 按锁定的自签名证书校验（是真校验，不是关掉校验）；
每个 HTTP 调用 5 s 超时，每张照片有 `--upload-timeout` 总时限。服务器上
的 `capture-list.txt`（每行一个文件名）由本地镜像维护：每次上传成功后
追加，累计 ≥8 个新名字且距上次推送 ≥30 s（或距上次 ≥5 分钟，先到为准）
时整文件重传，退出时冲刷；拉不到服务器列表时绝不推送，恢复后合并而非
覆盖，服务器端历史永不丢失。

上传需要配置文件（权限 600）：

```json
{
  "url": "https://capture.manjucc.com",
  "user": "Capturer",
  "password": "…",
  "ca": "/home/pi/.config/capturer/manjucc-capture.crt"
}
```

配置缺失或损坏 = 启动错误（退出码 1）。

### 浏览图库

照片在 <https://capture.manjucc.com/> 浏览（或只读入口
<https://manjucc.com/captures/>）。Basic Auth 用户 `Capturer`，密码即
`upload.json` 里的那个。浏览器会警告自签名证书——每台设备接受一次即可。

服务器端（HTTPS nginx、WebDAV、Basic Auth、`/var/www/captures` 目录
布局、每日照片保留 timer）由 `server/deploy-v5.sh` 幂等（重）建——完整
的设计 / 部署 / 使用文档见 `docs/https-upload.md`。

## 测试

```bash
# 上传器的离线端到端自测（内置 HTTPS+WebDAV 存根服务器）
python3 -I tests/local_upload_check.py

# 离线逻辑单测（不需要相机/torch/网络）
python3 -I tests/test_capture_logic.py

# 对单张图片做检测器 CLI 冒烟测试
python3 yolov5-USB/detect.py --weights yolov5-USB/yolov5s.pt \
    --source <图片> --classes 0
```

## 目录结构

```
capture.py        主程序：拍摄、检测、磁盘下限、上传器
yolov5-USB/       不入库——按上文从上游自行准备
tests/            local_upload_check.py — 上传器离线自测
                  test_capture_logic.py — 离线逻辑单测
server/           deploy-v5.sh — 图库服务器端的幂等（重）部署脚本
                  （装 90 天照片保留 timer；TLS 证书仅剩 <30 天时才重签）
docs/             https-upload.md — 完整设计/部署/使用文档
README.md         English README
CLAUDE.md         Claude Code 会话的工作区指南
```

## 实现要点

- **为什么用 worker 线程**：在这台 Pi 上，SIGINT 若落在主线程正在执行
  torch/OpenMP 原生调用的时刻，可能把进程卡死。因此拍摄循环跑在 worker
  线程上、用事件标志停止；主线程只做短暂可中断的 join，worker 停不下来
  就强制退出。
- **拍摄**：相机经 OpenCV V4L2 后端打开一次、整个运行期持有（仅 worker
  线程）；每轮先丢弃单个滞留的缓冲帧再取下一帧，单张约 0.1 s，而不是
  进程启动的约 1 s。读取失败会释放句柄、下一轮懒重开，该帧计入
  "failed"。帧以内存数组直接送检测——被丢弃的照片完全不落盘。
- max-runtime 时钟从模型加载**前**起算；拍摄网格在加载后对齐。
- 磁盘下限在拍摄循环里查，主线程看门狗也查——worker 即使卡死在原生
  代码里，下限依然生效。

## 许可证

AGPL-3.0-only（`LICENSE`）。

这是刻意的选择而非默认：本项目经 `torch.hub` 运行 YOLOv5 代码，而
YOLOv5 是 AGPL-3.0 许可（见
[ultralytics/yolov5 的 LICENSE](https://github.com/ultralytics/yolov5/blob/master/LICENSE)）
——强 copyleft，延伸到组合作品与网络使用（第 13 条）。与本依赖一致的
许可证只有 AGPL-3.0。
