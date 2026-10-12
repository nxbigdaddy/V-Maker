# 模型接入与任务执行

## 1. 设计目标

业务层不写死 DeepSeek、Qwen Image 或 H3。系统面向“能力”编程，Provider 适配器负责把统一请求翻译成厂商请求。

## 2. 能力接口

```text
TextProvider
  - generateStructured
  - chat
  - summarize

ImageProvider
  - textToImage
  - imageToImage
  - inpaint

VideoProvider
  - textToVideo
  - imageToVideo
  - startEndFrameToVideo
  - extendVideo

SpeechProvider
  - synthesize
  - align
  - cloneVoice（需额外授权控制）

MusicProvider
  - generate
  - extend

VisionReviewProvider
  - inspectImage
  - inspectVideo

ModerationProvider
  - checkText
  - checkMedia
```

不应把所有能力塞进一个含大量可选字段的 `generate()`。

## 3. 统一请求信封

```json
{
  "capability": "image_to_video",
  "model_ref": "provider/model@version",
  "inputs": {
    "image_asset_version_id": "av_...",
    "prompt": "..."
  },
  "parameters": {
    "duration_sec": 5,
    "aspect_ratio": "16:9"
  },
  "output_spec": {
    "count": 2,
    "container": "mp4"
  },
  "safety_policy": "project_default",
  "timeout_sec": 900,
  "idempotency_key": "...",
  "metadata": {
    "project_id": "prj_...",
    "shot_id": "shot_..."
  }
}
```

统一响应：

```json
{
  "provider_job_id": "...",
  "status": "submitted",
  "outputs": [],
  "usage": {"unit": "video_second", "quantity": 10},
  "estimated_cost": {"currency": "CNY", "amount": 3.2},
  "actual_cost": null,
  "warnings": [],
  "raw_response_ref": "secure-blob://...",
  "reproducibility": "best_effort"
}
```

## 4. 模型目录

每个模型登记：

- 支持能力；
- 最大提示词长度；
- 分辨率、时长和画幅限制；
- 是否支持参考图、尾帧、seed；
- 同步/异步调用方式；
- 定价和计量单位；
- 并发与速率限制；
- 内容策略；
- 商用许可备注；
- 区域与数据处理说明；
- 当前健康状态。

示例映射：

```yaml
capabilities:
  story_analysis:
    primary: deepseek/deepseek-chat
  screenplay:
    primary: deepseek/deepseek-reasoner
  image_generation:
    primary: qwen/qwen-image
  video_generation:
    primary: custom/h3
  visual_review:
    primary: qwen/qwen-vl
  speech:
    primary: configured-tts/default
```

这里只是配置示例，不应成为代码常量。

## 5. Provider 适配器职责

- 密钥和请求签名；
- 统一字段到厂商字段的映射；
- 本地资产上传；
- 异步任务提交、轮询和回调；
- 状态归一；
- 输出安全下载与哈希；
- 使用量和成本解析；
- 错误分类；
- 限流和熔断；
- 保存必要但已脱敏的原始响应。

## 6. 错误分类

| 类型 | 示例 | 自动处理 |
|---|---|---|
| transient | 429、5xx、网络超时 | 指数退避并遵守 Retry-After |
| input | 参数非法、尺寸不支持 | 不原样重试，修改请求或提示用户 |
| moderation | 内容被拒绝 | 标记镜头并要求安全改写 |
| auth | Key 无效、签名失败 | 暂停该 Provider 队列 |
| quota | 余额不足、限额用尽 | 阻塞并提示切换或充值 |
| provider_terminal | 厂商任务永久失败 | 可按路由策略切换 Provider |
| budget | 本地预算不足 | 禁止提交 |
| internal | 下载损坏、数据库错误 | 保留上下文后恢复/人工处理 |

只有 transient 默认自动原参数重试。

## 7. 路由策略

支持：

- 固定模型；
- 按质量优先；
- 按成本优先；
- 按延迟优先；
- 主备故障转移；
- 用户逐任务指定。

跨 Provider 重试可能改变风格，因此必须创建新 attempt 和候选版本，不能悄悄覆盖原结果。

## 8. Prompt 编译器

内部保存语义字段，最终由模型专属模板编译：

```text
Shot Spec + Bible Snapshot + Asset References
                  ↓
        Neutral Prompt Document
                  ↓
      Provider-specific Compiler
                  ↓
        Prompt + Native Parameters
```

编译文档应保存：

- 模板版本；
- 全局风格段；
- 角色/场景/道具段；
- 构图和动作段；
- 负面约束段；
- Provider 特定语法；
- 最终提示词。

这样更换模型时只需要重新编译，不必丢失创作语义。

## 9. 结构化 LLM 输出

LLM 任务必须：

1. 指定 JSON Schema；
2. 校验类型、枚举和引用 ID；
3. 执行业务 lint；
4. 可修复时发送最小错误信息让模型修复；
5. 超过次数后等待人工，不把无法解析的自然语言写入正式数据。

对故事和剧本的创意内容，可同时保存人类可读 Markdown 和规范化 JSON。

## 10. 任务执行协议

Worker 领取任务后：

1. 使用乐观锁把任务转为 `running`；
2. 解析不可变输入快照；
3. 再次检查预算、取消状态和 Provider 健康；
4. 创建 attempt；
5. 执行调用或轮询；
6. 将输出下载到临时目录；
7. 校验 MIME、可解码性和哈希；
8. 原子移动到资产库并注册版本；
9. 结算成本；
10. 创建 QC 任务；
11. 发出事件。

任何一步失败都保留 attempt 与诊断信息。

## 11. 并发和队列

分别设置：

- Provider 全局并发；
- 模型级每分钟/每天限额；
- 项目并发；
- 视频高成本队列；
- 本地 FFmpeg CPU/GPU 并发；
- 优先级和公平调度。

大量镜头生成采用 fan-out，但不能一次把所有任务提交给外部 Provider。调度器需要滑动窗口和背压。

## 12. 自动质检

### 图片

- 文件和尺寸；
- 人物身份与服装；
- 构图和人数；
- 手部/脸部异常；
- 水印和乱码；
- 风格匹配；
- 内容安全。

### 视频

- 可解码、时长和分辨率；
- 黑帧、冻结、闪烁；
- 面部漂移和异常肢体；
- 动作与镜头描述匹配；
- 首尾连续性；
- 是否包含不需要的新角色或文字。

### 音频

- 空音频、削波、底噪；
- ASR 与台词一致；
- 发音词典命中；
- 语速、情绪和时长；
- LUFS 和 true peak。

## 13. 诊断式重试

自动重试应根据失败原因变异：

- 身份不一致：提高参考权重或更换参考图；
- 动作失败：拆解动作、降低人物交互；
- 运镜错误：简化运镜；
- 闪烁：缩短镜头或换模型；
- TTS 误读：加入发音词典、拆句或 SSML；
- 时长不足：延长、冻结尾帧或重新生成。

配置字段：`max_attempts`、`max_cost`、`min_improvement`、`mutation_policy`。无明显改善时停止自动尝试。

## 14. 密钥管理

- API Key 放 OS Keychain；
- 数据库只存 key alias；
- 前端只能看到掩码；
- 日志和错误消息自动脱敏；
- 项目导出不包含密钥；
- 提供“测试连接”与最小权限建议；
- 声音克隆等敏感能力需额外授权标记。

## 15. 已实现：按能力区分的 Provider 配置（schema v7）

MVP 里文本、图像、语音、视频四种能力的配置是彼此独立的，落在 `capability_settings` 表：

```text
capability_settings(
  capability TEXT PRIMARY KEY CHECK(capability IN ('text','image','speech','video')),
  provider TEXT NOT NULL,          -- 'demo' 或 OpenAI 兼容服务标识
  endpoint TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  api_key_env TEXT NOT NULL DEFAULT '',
  unit_cost REAL NOT NULL DEFAULT 0,
  options_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
)
```

设计要点：

- **`unit_cost` 由用户自己填写**。系统不会猜测任何服务的价格；它只把用户填写的单次预估成本
  用于预算预留与硬上限判断。填 0 表示不预留。
- **默认三行都是 `demo`**，即本地占位生成器，不联网、零成本。
  升级旧库时，`text` 行会继承原 `provider_settings` 的配置，
  `image` / `speech` 初始化为 demo；已存在的能力行在重复启动时不会被覆盖。
- `configured` 是**派生字段**而非存储字段：`provider == 'demo'` 或对应环境变量确实存在于
  后端进程环境时为真。GUI 在 `provider != demo` 且 `configured == false` 时必须明确警告，
  不能假装已配置。

接口：

```text
GET /api/capability-settings
GET /api/capability-settings/{capability}
PUT /api/capability-settings/{capability}
```

`GET /api/provider-settings` 与 `PUT /api/settings/text-provider` 保留，并且读写的就是
`capability_settings` 里 `capability='text'` 的同一行，因此新旧接口不会各存一份配置。

## 16. 已实现：图像与语音适配器

```text
class ImageProvider(ABC):
    def generate(prompt: str, *, size: str = "1280x720") -> tuple[bytes, str]   # (数据, mime)

class SpeechProvider(ABC):
    def synthesize(text: str, *, voice: str = "") -> tuple[bytes, str]         # (音频数据, mime)
```

实现：

- `DemoImageProvider` / `DemoSpeechProvider`：本地占位（SVG 故事板、WAV 音调），产物一律标记
  `demo=True`，并带“本地占位，不是 AI 生成”的说明。
- `OpenAICompatibleImageProvider`：`POST {endpoint}/images/generations`，
  兼容 `data[0].b64_json` 与 `data[0].url`（用同一个 Authorization 头下载，限制 32 MiB）；
  mime 依次取响应字段、下载响应头、magic bytes，最后兜底 `image/png`。
- `OpenAICompatibleSpeechProvider`：`POST {endpoint}/audio/speech`，直接返回音频字节。
- `OpenAICompatibleVideoProvider`：**异步 submit / poll / fetch**，因为真实图生视频服务几乎都是
  「提交任务 → 轮询 → 取回成片」的形态，同步接口接不了：

  ```text
  POST {endpoint}/video/generations            → {"id"|"task_id": "..."}
  GET  {endpoint}/video/generations/{id}       → {"status":"queued|processing|succeeded|failed",
                                                  "progress":0.42,"message":"...","video_url":"...","error":"..."}
  GET  {video_url}                             → 成片字节（限制 256 MiB）
  ```

  **这是 V-Maker 自己定义的约定，不是任何厂商的标准接口**。对接可灵 / 火山 / Runway 等真实服务时
  需要按其文档另写适配器，这个形状只作为参考。未知的 `status` 值会当作 `processing` 并在消息里带上原值，
  不会被静默当成成功。
- `DemoVideoProvider`：本地有 FFmpeg 时，把该镜头已有的占位画面渲染成一个**真实、可解码的占位片段**
  （画布跟随项目宽高比，带一条移动标记以便一眼看出是占位），注册为 `demo` 素材并在元数据里写明"不是 AI 生成"。
  没有可用 FFmpeg 时 `submit()` 抛 `ProviderError` 并说明缺什么。**它绝不产出一个假视频素材**——
  占位片段是真的能解码、也真的被标记为占位，而不是伪装成生成结果。

诚实性约束（已实现并有测试覆盖）：

1. Provider 失败时任务直接 `failed`，**绝不回退成本地占位产物**，也不会留下半成品 asset；
2. 真实产物写入资产时 `demo=False`，metadata 记录 `provider` / `model` / `prompt`（或 `voice`），
   GUI 据此显示「AI 生成」或「DEMO / 本地占位 · 不是 AI 生成」；
3. 所有 ProviderError 消息在抛出前都会用实际 API key 做一次脱敏替换，
   即使上游把 key 回显在错误体里也不会泄漏到任务记录或界面；
4. **连接测试不对视频做真实生成**：一次真实调用成本高且可能耗时数分钟，
   `live=true` 时也只验证配置与 endpoint 连通性，并在返回消息里写明「未做真实生成测试」。

## 17. 已实现：镜头级媒体任务

```text
POST /api/projects/{id}/shots/{shot_id}/image   → 单镜头生成画面
POST /api/projects/{id}/shots/{shot_id}/video   → 单镜头图生视频
POST /api/projects/{id}/media-images            {"shot_ids": [...]} 默认项目全部镜头
POST /api/projects/{id}/media-videos            {"shot_ids": [...]} 默认项目全部镜头
POST /api/projects/{id}/speech                  {"shot_ids": [...]} 默认项目全部镜头
```

- 任务类型为 `shot_image` / `shot_video` / `speech`，沿用通用任务状态机与进度上报；
- **每个镜头开始前重新读取任务状态**，被取消则立即停止，不会继续消耗额度；
- 视频是异步的，因此**每次轮询前也会重新读取任务状态**：一旦不再是 `running` 就立即返回，
  既不登记素材也不继续轮询。轮询间隔 `VMAKER_VIDEO_POLL_SECONDS`（默认 3），
  总超时 `VMAKER_VIDEO_TIMEOUT_SECONDS`（默认 600），超时按失败处理且不留素材；
- 图生视频以**该镜头已有的最新图像素材**作为首帧（读取时校验 SHA-256）；
  镜头没有图像时在提交任何请求**之前**就失败，不会浪费一次真实调用；
- `estimated_cost = unit_cost × 镜头数`，在 `claim()` 阶段参与预算硬上限判断；
- 图像 prompt 由镜头字段（动作、景别、机位角度、运镜）加上场景与项目视觉风格拼装，
  拼装函数独立可测；视频 prompt 在图像 prompt 之上叠加运镜意图。
