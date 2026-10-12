# V-Maker

V-Maker 是一个本地优先的 AI 动画制作工作台。当前 MVP 已打通：

```text
故事输入 → 故事分析 → 项目圣经 → 剧本/分镜 → Demo 媒体
→ Neutral Timeline → MP4（有 FFmpeg）或通用草稿包
```

完整设计见 [设计文档](./docs/README.md)。
**怎么验证它真的能跑、以及哪些部分明确没验证过**，见 [验证清单](./docs/12-verification-checklist.md)。

## 当前可用能力

- React + TypeScript 中文 GUI；
- FastAPI + SQLite 本地服务；
- 完整项目创建参数和安全数据库迁移；
- Demo 或 DeepSeek/OpenAI-compatible 故事分析；
- 项目圣经生成、版本与批准；
- 剧本、场景、镜头生成、编辑与批准；
- 不可变媒体资产版本、SHA-256 和镜头血缘；
- 明确标注“NOT AI”的离线 PNG/WAV/SRT 占位素材（位图而非 SVG，因此能直接进 MP4 与剪映草稿）；
- Neutral Timeline；
- FFmpeg MP4 输出，画布跟随项目宽高比（16:9 / 9:16 / 1:1），已实机验证可解码；
  可选依赖 `imageio-ffmpeg` 自带 ffmpeg，不装也能通过 `VMAKER_FFMPEG_PATH` 或系统 PATH 提供；
- FFmpeg 不可用、或素材不是位图（如 SVG 占位图）时如实回退为真实可携带草稿 ZIP，绝不冒充 MP4；
- 真实剪映草稿导出（可选依赖 `pyJianYingDraft`），不可用时如实回退并说明原因；
- **demo 视频占位片段**：视频能力仍为 demo 时，后端用本机 FFmpeg 把镜头已有画面渲染成真实可解码的
  MP4（带移动标记、标记为 demo，明确不是 AI 生成）；没有可用 FFmpeg 时如实失败，绝不产出假素材；
- Tauri 2 桌面壳**配置**（图标已补、配置有静态检查；但**从未构建过**：本机没有 Rust 与 MSVC
  生成工具，且后端 sidecar 尚未捆绑，详见 `docs/11-ui-layout-regression.md`）；
- 持久任务中心，支持取消、重试、启动恢复；
- 成本账本、预算预留与硬上限；
- 项目复制、归档、恢复和软删除；
- 安全的导出产物下载与系统环境诊断；
- 一键自动制作流水线，带审核门、断点续跑与取消；
- 素材内容服务（支持 Range），GUI 内可直接预览图片与试听音频；
- 按能力（文本 / 图像 / 语音 / **视频**）区分的 Provider 配置；
- OpenAI 兼容的图像与语音适配器，以及镜头级生成任务；
- **视频能力走异步 submit/poll**：提交任务 → 轮询进度 → 下载成片，支持取消、超时与预算控制；
- **真实剪映草稿导出**（可选依赖 `pyJianYingDraft`）：产出 `draft_content.json` / `draft_meta_info.json`，
  库缺失、构建失败或产出为空时**如实回退**为通用草稿包并说明原因；
- 进程内单并发任务队列执行器（重启后自动续跑中断的任务）；
- 存储用量统计与安全的孤儿文件清理。

> 当前没有伪装云端生图、生视频或 TTS。离线媒体是用于验证完整生产链路的占位内容。真实 Qwen Image/H3/TTS 仍需在 Provider 层接入具体 API。

## 开发环境

- Node.js 20+
- pnpm 9+
- Python 3.12+
- FFmpeg（可选；没有时仍可输出草稿包）
- Rust stable + Windows WebView2（仅 Tauri 桌面壳需要）

## 安装

```powershell
pnpm install
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## 运行

终端一：

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765 --reload
```

终端二：

```powershell
pnpm dev:web
```

打开 `http://localhost:1420`。

也可以在已配置 Python 和 pnpm 的 PowerShell 中运行：

```powershell
pnpm dev:all
```

Windows 也可以直接双击项目根目录的 `start-vmaker.bat`。它会检查 Python、pnpm 和依赖，启动前后端，并自动打开 `http://localhost:1420`。

运行环境诊断：

```powershell
pnpm doctor
```

诊断会检查 Python、后端依赖、pnpm、前端依赖，以及可选的 FFmpeg、Rust/Cargo。

## 完整体验路径

手动分步制作：

1. 新建项目并输入故事；
2. 打开“故事与剧本”，运行故事分析；
3. 打开“项目圣经”，生成并批准角色、场景和道具；
4. 打开“分镜工作台”，生成生产计划，编辑并批准镜头；
5. 打开“素材库”，生成明确标注为 Demo 的离线占位素材；
6. 打开“时间线”，生成 Neutral Timeline；
7. 打开“导出”，生成 MP4 或通用草稿包。

一键自动制作：

在工作台点击“继续自动制作”，后端会创建一个 `pipeline` 任务并按顺序执行：

```text
故事分析 → 项目圣经 → 剧本与分镜 → 素材 → 时间线 → 导出
```

默认开启审核门。流水线会在「项目圣经」和「剧本与分镜」之后暂停，任务状态变为 `paused`，
消息形如「「项目圣经」已完成，请审核后再继续制作」。此时：

- 工作台与任务中心会显示真实的待审核提示；
- 点“继续”会调用 `POST /api/tasks/{id}/resume?execute=true`，后端随即从断点续跑；
- 点“取消”会立即停止，之后任务只能重试，不能恢复；
- 阶段进度记录在任务的 `payload_json`（`stage_index` / `completed_stages`），中途重启也能续跑。

也可以直接用 API：

```text
POST /api/projects/{id}/pipeline          {"require_review": true}
GET  /api/tasks/{id}
POST /api/tasks/{id}/resume?execute=true
POST /api/tasks/{id}/cancel
```

`require_review` 设为 `false` 时不在审核门停留，一次请求即可跑完整条流水线。

## 素材预览
素材库不再只显示文件路径。后端提供按项目校验、并逐次校验 SHA-256 的内容接口：

```text
GET /api/projects/{id}/assets/{asset_id}/versions/{version_id}/content
GET /api/asset-versions/{version_id}/content
```

- 响应为 `inline`，带 `nosniff` 与严格 CSP（SVG 被直接打开时不会执行脚本）；
- 支持 HTTP Range，音频可在浏览器里拖动播放；
- 跨项目、已软删除的项目、伪造 ID、文件被篡改一律返回 404。

## 真实媒体生成（图像 / 语音）

MVP 默认只产出**明确标注的本地占位素材**。要接入真实服务，请在「模型设置」里按能力分别配置：

```text
文本 / 图像 / 语音  →  provider: demo 或 openai（OpenAI 兼容接口）
                      endpoint、model、API Key 环境变量、单次调用预估成本
```

对应接口：

```text
GET /api/capability-settings
GET /api/capability-settings/{capability}      capability = text | image | speech
PUT /api/capability-settings/{capability}
```

设计原则：

- **系统不猜测价格**。`unit_cost` 由你自己填写，仅用于预算预留与硬上限判断；填 0 表示不预留。
- **失败不降级**。Provider 报错时任务直接失败，绝不会悄悄退回成本地占位素材，也不会留下半成品资产。
- **来源可辨**。真实产物写入资产时 `demo=false` 并记录 `provider` / `model` / `prompt`，界面显示「AI 生成」；
  本地占位素材显示「DEMO / 本地占位 · 不是 AI 生成」。
- **密钥不外泄**。密钥只从后端进程环境变量读取，从不写入数据库；所有 Provider 错误消息在抛出前都会做一次脱敏替换。

### 付费前先「测试连接」

```text
POST /api/capability-settings/{capability}/test
body: { "live": false, "provider"?, "endpoint"?, "model"?, "api_key_env"? }
```

- 用**尚未保存**的表单值测试，方便先验证再保存；不会写库。
- `live=false`（默认，界面上的「测试连接」）**不调用生成服务、不产生费用**：
  只检查密钥环境变量是否存在，并对 endpoint 主机做一次 5 秒 TCP 连通性检查。
- `live=true`（界面上的「真实调用测试」，需二次确认）会对文本发一次 `max_tokens: 1` 的极小请求、
  对语音发一次极短输入的请求；**图像能力不做真实生成测试**（避免产生一整张图片的费用），
  这一点由后端在 `message` 里如实说明。
- 响应恒为 200，用 `ok` / `configured` / `reachable` / `message` 表达结果；失败原因可直接展示，
  且不会包含 API Key。`reachable` 对 `demo` provider 为 `null`（不联网，无从谈起）。

镜头级生成任务：

```text
POST /api/projects/{id}/shots/{shot_id}/image   单镜头生成画面
POST /api/projects/{id}/media-images            {"shot_ids": [...]} 默认项目全部镜头
POST /api/projects/{id}/speech                  {"shot_ids": [...]} 默认项目全部镜头
```

`estimated_cost = unit_cost × 镜头数`；每个镜头开始前与 Provider 返回后都会重新检查任务状态，
因此在飞行中的镜头被取消时不会落盘。

> 尚未验证：本仓库没有对接过任何真实账号。当前适配器实现的是**同步** OpenAI 兼容形态
> （`/images/generations` 直接返回 `b64_json` 或 `url`）。以 DashScope `wanx` 为代表的
> **异步任务型**图像接口（先返回 task_id 再轮询）需要另写专用适配器，直接指向它很可能失败。

## DeepSeek 配置

默认 `demo` 分析器不调用外部 AI。如需 DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY="你的密钥"
```

然后在 GUI 模型设置中选择：

```text
Provider: DeepSeek
Endpoint: https://api.deepseek.com/v1
Model: deepseek-chat
API Key 环境变量: DEEPSEEK_API_KEY
```

密钥只从后端进程环境变量读取，不写入数据库或前端存储。

## FFmpeg

若 `ffmpeg` 已在 PATH 中，导出会尝试生成 MP4。也可指定：

```powershell
$env:VMAKER_FFMPEG_PATH="C:\tools\ffmpeg\bin\ffmpeg.exe"
```

若不可用或渲染失败，系统会诚实地返回 `completed_with_warnings`，并生成包含下列内容的 ZIP：

- `timeline.json`
- `subtitles.srt`
- `shot-list.csv`
- `manifest.json`
- SVG/WAV 媒体

这不是原生剪映草稿。剪映格式需要针对一个明确版本做单独适配；当前通用包是稳定降级出口。

## 测试与构建

一条命令跑全量测试（前端类型检查 + 前端单测 + 生产构建 + 后端 pytest）：

```powershell
pnpm test:all
```

也可以分开跑：

```powershell
pnpm test          # 前端类型检查 + vitest
pnpm test:web      # 只跑前端单测（vitest run）
pnpm build:web     # 生产构建
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q
```

`pnpm test:all` 调用 `scripts/test-all.ps1`，它不依赖 PATH 上的 pnpm，直接使用仓库内的本地二进制；
每一步都会跑完再汇总，不会一失败就中断。后端 python 可用 `VMAKER_PYTHON` 指定。

当前执行结果：前端类型检查与生产构建通过、**前端 26 项单测通过**、**后端 85 项测试通过**。

前端单测（`src/test/`）覆盖的是真实出过问题的地方，不是为凑数：

- `dialog.test.tsx` —— `useDialog` 的 Esc / 焦点圈定 / `active` 语义（关闭状态下不得锁死页面滚动）；
- `client.test.ts` —— 错误信封解析、204、URL 编码，以及 **retry/resume 必须带 `?execute=true`**
  （曾经漏掉它，任务排进队列后永远没人执行）；
- `settings-modal.test.tsx` —— 模型设置加载必须结束并渲染后端返回的值
  （曾经因为 effect 依赖自身状态而永远停在「正在读取配置」）；
- `project-card.test.tsx` —— 卡片与菜单的键盘/点击互不误触。

数据库 schema 版本为 `v6`（新增按能力区分的 `capability_settings`，以及任务 `paused` 审核状态）。

可选的本机联调脚本（需要后端已在 `127.0.0.1:8765` 运行）：

```powershell
python scripts/check_live_pipeline.py        # 一键流水线 + 素材内容服务 + Range
python scripts/check_live_review_gate.py     # 审核门暂停与 resume 续跑
python scripts/check_live_media_provider.py  # 用本地 mock 图像服务验证真实生成链路
python scripts/check_migration_v5.py         # schema v5 迁移幂等性
python scripts/check_migration_v6.py         # 能力设置 seed 与不覆盖语义
python scripts/check_live_db_v6.py           # 在真实数据库副本上演练 v5 → v6 升级
python scripts/check_live_probe.py           # 「测试连接」接口的实机行为（含不写库与 404/422）
python scripts/check_live_worker.py prepare  # 造一个「中断的」任务，之后重启后端
python scripts/check_live_worker.py verify   # 断言队列 worker 自动把它跑完（全程不调 /execute）
```

除最后一个（只读副本）外，这些脚本会创建真实项目和任务，并在结束时软删除它们。

### UI 布局自动检测

`scripts/ui-probe.html` 是一个同源 iframe 探针：它会在多个「视口宽度 × 视口高度 × 界面状态」组合下
加载真实应用，遍历 DOM 并报告

- 元素超出视口（且没有可滚动祖先兜底）；
- 同一父元素下兄弟节点的包围盒相交（排除正常嵌套、装饰性 hero、SVG 内部元素与同一行盒的行内文本）。

跑法（需要开发服务器已在 1420 运行，探针必须与前端同源才能读取 iframe DOM）：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --headless=new --disable-gpu --user-data-dir="$env:TEMP\probe" `
  --window-size=1500,1000 --virtual-time-budget=900000 `
  --dump-dom "http://127.0.0.1:1420/scripts/ui-probe.html"
```

在输出里看 `<pre id="report">` 的内容。当前基线：3 个宽度 × 3 个高度 × 16 个界面状态全部正常。

覆盖的状态包括首页、新建向导（含末步）、模型设置（两个标签页）、项目菜单、删除确认框，
以及工作台的 9 个页面。这个探针在实际使用中定位到了三处真实的布局缺陷（向导 footer 被正文压住、
`.review-item` 标题与说明连成一行），比逐个看截图可靠。

`check_live_media_provider.py` 需要后端进程带一个测试密钥环境变量启动，例如：

```powershell
$env:VMAKER_TEST_IMAGE_KEY="sk-live-mock-image-key-987654321"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

它会把「图像」能力临时指向本地 mock 服务并在结束时恢复为 `demo`。

## 桌面应用

安装 Rust stable 后：

```powershell
pnpm tauri:dev
```

当前 Tauri 桌面壳尚未捆绑 Python sidecar，因此开发阶段仍需单独启动后端。这是已知的部署限制，不影响 Web GUI 与本地 API 的完整 MVP 流程。

## 数据位置

默认数据库：`backend/data/vmaker.db`

项目媒体与导出：

```text
backend/data/projects/<project_id>/
├── assets/
├── timelines/
└── exports/
```

API 默认只监听 `127.0.0.1:8765`。

## 任务队列执行器

后端进程内有一个单并发的工作线程（`backend/worker.py`），持续把 `queued` 状态的任务取出来执行。

它修掉的是一个真实缺陷：任务原本只靠创建请求上的 `BackgroundTasks` 执行，因此

- 进程重启后，`recover_running()` 会把中断的任务放回 `queued`，**但没有任何东西会去跑它**；
- `retry` / `resume`（不带 `?execute=true`）把任务置回 `queued` 后，同样会永远停在"排队中"。

现在这两类任务都会被 worker 自动接手。配置：

```powershell
$env:VMAKER_WORKER_ENABLED="0"        # 关闭队列消费（默认开启）
$env:VMAKER_WORKER_POLL_SECONDS="1"   # 轮询间隔，默认 1 秒
$env:VMAKER_WORKER_STALE_SECONDS="30" # 排队超过这么久就由 worker 接管，默认 30 秒
```

- **单并发**：同一时刻只执行一个任务，不会同时轰击外部服务；
- 某个任务失败不会让线程退出，错误记进 `stats()['last_error']`（已脱敏）；
- **不重复执行**：本进程新建的任务默认交给 `BackgroundTasks`，worker 只认领"本进程启动前就存在"的任务；
  但一个任务如果**排队超过 `stale_seconds` 仍未被执行**（说明 BackgroundTasks 没跑起来），
  worker 会接管它 —— 否则新建任务也可能永远停在"排队中"，那正是它要消灭的缺陷；
- 状态可在 `GET /api/system/diagnostics` 的 `worker` 字段看到：
  `{ enabled, running, poll_interval, executed, failed, last_task_id, last_error, last_finished_at }`。

实机验证（模拟断电后重启，全程不调用 `/execute`）：

```text
task status: completed | attempts: 2
events: ['recovered', 'started', 'progress', 'progress', 'completed']
```

脚本：`python scripts/check_live_worker.py prepare` → 重启后端 → `python scripts/check_live_worker.py verify`。

## 存储用量与清理

媒体文件只增不减（重新生成镜头会新建 asset、删除项目只是软删除），所以需要能看到并回收：

```text
GET  /api/system/storage            # 总量、按项目占用、孤儿文件、软删除项目占用
POST /api/system/storage/cleanup    # {"dry_run": true, "include_deleted_projects": false}
```

"引用"的定义是严格且保守的，只有下列文件才不会被当作垃圾：

1. `asset_versions.relative_path` 指向的文件；
2. `export_artifacts.relative_path` 指向的交付文件；
3. `timeline_versions` 对应的 `timelines/v<version>.json`
   —— 它没有被任何表记录，但 `get_timeline` 仍然把它作为 `relative_path` 返回，
   按"孤儿"删掉会让接口指向一个不存在的文件；
4. 状态为 `running` 的导出任务目录 `exports/<job_id>/` 下的全部文件
   —— `clip-*.mp4`、`concat.txt` 这些中间产物没有登记，删掉会打断正在进行的渲染。

安全约束：默认 `dry_run=true`；每个目标在删除前**逐条重查数据库**；只删除解析后仍在 projects 根目录内的普通文件
（越界计入 `skipped_unsafe`，符号链接不会被跟随）；单个文件删除失败不影响其余文件；`targets` 最多返回 200 条。

实机结果（当前工作区）：`total_bytes≈5.4MB`、`file_count=189`、`orphan_files=0`、软删除项目文件 159 个。
`orphan_files=0` 正是上面第 3 条生效的证据。

---

## 许可证

本项目采用 [MIT 许可证](LICENSE)。

## 第三方与上游声明

本项目包含或依赖第三方组件，其版权与许可证归各自作者所有，
详见仓库内的依赖清单与相关声明文件。若本项目基于其他开源项目二次开发，
已在正文中注明上游项目与所基于的版本，本项目为**非官方**衍生作品。

## 现状与限制

- 本项目为个人开发项目，按“现状”提供，不附带任何明示或暗示的担保。
- README 与代码注释中记录的验证结论仅针对当时的具体环境与版本，不代表在其他环境下同样成立。
- 涉及本地凭据的部分统一使用系统凭据库或环境变量，仓库内不含真实密钥；
  示例配置仅使用占位符。
