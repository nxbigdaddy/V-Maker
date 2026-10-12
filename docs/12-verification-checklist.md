# V-Maker 验证清单

这份清单把「怎么证明它真的能跑」固化成可复现的命令，并**明确写出每条结论不覆盖什么**。
不要用某一条检查的通过去替代另一条。

## 1. 一条命令跑完全部自动化检查

```powershell
pnpm test:all          # 或 powershell -ExecutionPolicy Bypass -File scripts/test-all.ps1
```

| 步骤 | 证明什么 | 不证明什么 |
| --- | --- | --- |
| 脚本编码检查 | 含非 ASCII 的 `.ps1` 都带 UTF-8 BOM；`.bat` 必须是 CRLF 且无 BOM（LF 会让 cmd 吃掉行首字符，BOM 会让 `@echo off` 失效） | — |
| 前端类型检查 `tsc -b` | 类型层面自洽 | 运行时行为 |
| 前端单元测试 `vitest` | 组件与 API 客户端的行为（含失败态文案、任务状态按钮、导出格式） | 真实浏览器布局 |
| 前端生产构建 `vite build` | 产物能构建出来 | 产物能运行（见第 3 节） |
| Tauri 配置静态检查 | 图标/脚本/版本/窗口/权限引用都自洽 | **桌面应用能构建或运行**（需要 Rust + MSVC，见第 5 节） |
| 后端测试 `pytest` | 导出、迁移、任务、provider、剪映草稿与 MP4 校验等逻辑 | 真实付费 provider 的兼容性 |

需要 Python 时用 `-e ".[dev,ffmpeg,jianying]"` 装齐依赖；`VMAKER_PYTHON` 可指定解释器。

## 2. 浏览器布局与点击命中

```powershell
# 开发服务器（1420）
./scripts/check-ui.ps1 -Widths '320,360,420,480,700,760,1000,1280' -Heights '420,560,760'

# 生产构建（先 pnpm build:web；-StageProbe 会把探针临时放进 dist，跑完自动删除）
./scripts/check-ui.ps1 -BaseUrl 'http://127.0.0.1:4173' -StageProbe -Widths '420,760,1280' -Heights '760'

# 后端不可达（真实连接被拒）：另起一个把 API 指向死端口的开发服务器，并带 -Offline
$env:VITE_API_BASE_URL='http://127.0.0.1:9/api'
node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 1424 --strictPort
./scripts/check-ui.ps1 -BaseUrl 'http://127.0.0.1:1424' -Offline -Scenarios '首页·后端不可用' -ExpectText 'http://127.0.0.1:9'
```

探针在真实 DOM 上做几何与命中检查：溢出、跨父元素控件互撞、按钮内文字/图标越界、逐个按钮
`scrollIntoView` + `elementFromPoint` 遮挡检查。**它不做人工截图审美判断**，也不覆盖所有
loading/error/任务状态分支；`首页·后端不可用` 只在 `-Offline` 时注册，因为后端正常时它注定失败。

夹具说明：部分场景依赖已有数据（带暂停任务、带失败任务、有导出记录的项目）。
`scripts/make_error_state_fixture.py [force]` 可重建这些真实失败夹具（失败任务＝demo 视频
provider 如实失败；失败导出＝改动素材一个字节触发 SHA-256 拒绝，随后立即还原素材文件）。

## 3. 导出链路的实机验证

```powershell
# 纯 demo 项目（不配置任何 provider）：剪映草稿是否非空、MP4 是否真实可解码
python scripts/check_live_jianying_demo.py

# MP4 单独验证：画布跟随宽高比、完整解码、时长匹配（VMAKER_CHECK_PURE_DEMO=1 时不配 provider）
$env:VMAKER_CHECK_RATIO='9:16'; $env:VMAKER_CHECK_PURE_DEMO='1'; python scripts/check_live_mp4.py

# 带 mock 图像服务的正向/回退对照（需要 VMAKER_TEST_IMAGE_KEY，不产生真实费用）
python scripts/check_live_jianying.py

# demo 视频能力：本地占位片段 + 剪映草稿引用真实视频素材（不需要任何 provider）
python scripts/check_live_demo_video.py
```

已验证到的程度：**位图素材**下产出可解码的 H.264+AAC，画布 16:9→1920x1080 / 9:16→1080x1920 /
1:1→1080x1080，时长与时间线一致；素材不是位图（SVG）时如实回退成草稿包并给出可执行原因。
**没有验证**：真实付费图生视频/图像服务、真实剪映打开草稿、更长的成片与更多素材组合。

## 4. 用户入口与服务管理

```powershell
start-vmaker.bat doctor    # 启动失败时先跑这个：解释器、依赖、端口、管理器状态、日志末尾
start-vmaker.bat check     # 只自检：解释器、核心依赖、可选导出能力
start-vmaker.bat backend   # 只启动 API（供桌面壳使用）
start-vmaker.bat           # 一体化：后端 + 开发前端 + 打开浏览器

# 服务管理（启动器会把 stop/status/restart/logs 直接转给它）
./scripts/service.ps1 status    # 状态、PID、端口、schema、FFmpeg、剪映；未运行时退出码非 0
./scripts/service.ps1 start     # 启动（已在监听则复用），等待 /api/health
./scripts/service.ps1 stop      # 停止（PID 文件 + 按端口兜底），等端口真的释放
./scripts/service.ps1 restart
./scripts/service.ps1 logs      # 末尾 stdout + stderr
./scripts/service.ps1 check     # 自检 + 状态
```

管理器拥有进程：PID 写在 `backend/data/backend.pid`，stdout/stderr 落在 `backend/data/logs/`，
管理器自己的输出落在 `service.log`。`VMAKER_PYTHON` 会沿用它启动时用的解释器，避免重启时换到一个缺少可选依赖的 Python。

界面里也能做：**环境诊断 → 后端进程**显示 PID/端口/是否受管/日志路径，并提供「重启后端」「停止后端」
（停止有不可逆提示）。**「启动」不能从界面发起**——后端都停了，没有 API 可接收这个请求。

`check` 会报告可选依赖是否齐备——`imageio-ffmpeg` 缺失时 MP4 只会回退。
注意：`backend` 模式通过管理器用 `Start-Process` 拉起隐藏窗口；早期版本用 `start cmd /k` 会留下
无法查询、也无法可靠停止的窗口，这正是管理器要解决的问题。

## 5. 明确未验证的部分

| 项 | 状态 | 原因 |
| --- | --- | --- |
| Tauri 桌面应用构建/运行/打包 | **未验证** | 本机没有 Rust，也没有 MSVC 生成工具 + Windows SDK（`pnpm tauri info` 明确报出） |
| 后端 sidecar（桌面自动拉起后端） | **未实现** | 需要 Rust 构建；目前桌面壳打开后须自行先启动后端 |
| 真实付费 provider（文本/图像/语音/视频） | **未验证** | 没有真实账号；视频协议是 V-Maker 自定义的，不是任何厂商标准 |
| demo 视频的画面是否有意义 | **不是生成结果** | demo 视频是本地 FFmpeg 渲染的占位片段（移动标记 + 该镜头已有画面），它让链路可跑通，但内容不是为故事生成的 |
| 真实剪映能否打开生成的草稿 | **未验证** | 本机没有剪映；只验证了 JSON 结构与素材绝对路径 |
| 长成片、多素材、极端时长的导出 | **未验证** | 实机验证只覆盖约 10s、2–6 个镜头 |
| 失败/错误态的真实样本 | **部分覆盖** | 目前只有「任务失败」与「导出失败」两类真实夹具，其余用替身或 DOM 夹具 |

## 6. 结论措辞的自我约束

- 「已实机验证」只用于**真的运行过并观察到结果**的事；
- 「测试通过」不等于「功能可用」——跳过过的测试曾经掩盖过一条预期根本不可能成立的用例；
- 变异验证（故意破坏后确认检查会失败）是判断一条检查是否有效的默认手段；
- 任何一处"看起来对"都要有对应证据，否则写进第 5 节的未验证表。
