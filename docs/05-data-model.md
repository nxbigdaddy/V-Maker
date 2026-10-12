# 核心数据模型

## 1. 领域层级

```text
Workspace
└── Project
    ├── Project Bible
    │   ├── Character
    │   ├── Location
    │   ├── Prop
    │   ├── Style Guide
    │   └── Voice Profile
    ├── Season（可选）
    │   └── Episode
    │       └── Scene
    │           └── Shot
    ├── Assets / Asset Versions
    ├── Workflow Runs / Tasks
    ├── Timeline / Timeline Versions
    └── Exports
```

## 2. ID、版本和软删除

- ID 使用 UUIDv7 或 ULID；
- 可编辑实体包含 `revision` 乐观锁字段；
- 内容版本不可原地覆盖；
- 删除默认写入 `deleted_at`；
- 被批准或导出引用的版本不得物理删除；
- `current_version_id` 只是当前指针，历史版本仍保留。

## 3. 主要实体

### Project

```json
{
  "id": "prj_...",
  "name": "三打白骨精",
  "mode": "standard",
  "language": "zh-CN",
  "aspect_ratio": "16:9",
  "resolution": [1920, 1080],
  "fps": {"num": 25, "den": 1},
  "budget": {"currency": "CNY", "hard_limit": 300},
  "status": "active"
}
```

### Episode / Scene / Shot

Shot 是最小生成、审核、成本和重跑单元：

```json
{
  "id": "shot_...",
  "scene_id": "scene_...",
  "order": 20,
  "purpose": "悟空察觉妖气",
  "duration_ticks": 100,
  "shot_size": "medium_close_up",
  "camera_angle": "low",
  "camera_motion": "slow_push_in",
  "character_refs": [
    {"entity_id": "char_wukong", "version_id": "charv_2"}
  ],
  "location_ref": {"entity_id": "loc_mountain", "version_id": "locv_1"},
  "action": "悟空停步并抬头警觉地望向山谷",
  "emotion": "alert",
  "dialogue_refs": [],
  "render_strategy": "image_to_video",
  "continuity": {
    "screen_direction": "left_to_right",
    "prop_state": ["staff_in_right_hand"]
  },
  "status": "approved"
}
```

时间字段不用浮点秒。内部使用统一 tick 或有理数；导入导出时转换。

### Project Bible

`bible_version` 是一份可发布快照，各条目仍可独立版本化。镜头记录实际引用版本，避免“当前圣经变化导致旧片无法复现”。

### Dialogue Line

```json
{
  "id": "dlg_...",
  "speaker_character_id": "char_wukong",
  "text": "师父，前面有妖气。",
  "emotion": "alert",
  "locked": true,
  "estimated_duration_ms": 2800,
  "voice_profile_version_id": "voicev_3"
}
```

## 4. 资产模型

`asset` 表示逻辑资产，`asset_version` 表示具体不可变文件或文档版本。

### Asset

```json
{
  "id": "asset_...",
  "kind": "video",
  "semantic_role": "shot_render",
  "project_id": "prj_...",
  "status": "active",
  "current_version_id": "av_..."
}
```

### Asset Version

```json
{
  "id": "av_...",
  "asset_id": "asset_...",
  "version": 3,
  "uri": "assets/generated/ab/cd/hash.mp4",
  "sha256": "...",
  "mime": "video/mp4",
  "width": 1920,
  "height": 1080,
  "duration_ticks": 104,
  "frame_rate": {"num": 25, "den": 1},
  "source_task_attempt_id": "attempt_...",
  "approval_status": "approved",
  "license": {"source": "provider-x", "commercial_use": "unknown"}
}
```

### Asset Relation

用来记录：

- `derived_from`；
- `reference_of`；
- `depicts_character`；
- `audio_for_dialogue`；
- `proxy_of`；
- `replaces`。

这使系统可以回答“这段视频由哪张图、哪个角色版本和哪个提示词生成”。

## 5. 工作流与任务

### Workflow Run

代表一次“生成动态分镜”“正式生成本集”等用户意图。

### Task Node

保存节点定义、依赖、输入契约、预算和策略。

### Task Attempt

每次实际执行产生独立 attempt：

```json
{
  "id": "attempt_...",
  "task_id": "task_...",
  "number": 2,
  "provider": "example",
  "model": "video-model-v1",
  "normalized_request_ref": "blob://...",
  "provider_job_id": "remote_123",
  "status": "running",
  "estimated_cost": 1.5,
  "actual_cost": null,
  "idempotency_key": "..."
}
```

任务成功和内容批准是不同状态。

### 已实现的 Tasks 表（schema v5）

MVP 把上面的模型收敛为单表 `tasks` 加 `task_events` 审计流：

```text
tasks(
  id, project_id, task_type,
  status CHECK IN (queued, running, paused, completed, failed, canceled),
  payload_json, result_id, error, attempts,
  progress REAL, message, capability,
  estimated_cost, actual_cost, provider, model,
  created_at, updated_at, started_at, finished_at
)
```

- `paused` 于 schema v5 引入，专用于流水线审核门（阶段已产出、未获人工确认）；
- v4 及更早版本用 `pending` 表示排队，升级时由迁移脚本改写为 `queued`。SQLite 无法修改
  CHECK 约束，因此迁移会重建 `tasks` 表；重建脚本在事务外切换 `PRAGMA foreign_keys`，
  否则 `DROP TABLE` 会级联删掉 `task_events`；
- `payload_json` 对 `pipeline` 任务保存 `stages`、`require_review`、`stage_index`、
  `completed_stages`，用于断点续跑；
- 每次状态变化都会向 `task_events` 追加一条 `event_type` + `message`，构成可追溯的时间线：
  `created / started / progress / stage_completed / paused / resumed / retried / budget_rejected /
  completed / failed / canceled / recovered`。

### 已实现的 capability_settings 表（schema v7）

文本、图像、语音、视频四种能力的 Provider 配置各自独立成行：

```text
capability_settings(
  capability TEXT PRIMARY KEY CHECK(capability IN ('text','image','speech','video')),
  provider TEXT NOT NULL,
  endpoint TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  api_key_env TEXT NOT NULL DEFAULT '',
  unit_cost REAL NOT NULL DEFAULT 0,      -- 由用户填写；系统不猜测价格
  options_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
)
```

- v7 只是把 `video` 加进 CHECK 约束。SQLite 无法修改 CHECK，因此与 v5 的 `tasks` 一样**原地重建**该表
  （建新表 → `INSERT ... SELECT` 全量搬迁 → 换名），用户的既有配置行逐列保留；
- 首次升级时 `text` 行继承旧 `provider_settings` 的配置，`image` / `speech` / `video` 初始化为 `demo`；
  重复启动只会补齐缺失的能力行，**绝不覆盖用户已保存的配置**；
- `video` 的 demo 默认值是 `demo-ffmpeg-v1`：本地有 FFmpeg 时，它把该镜头已有的占位画面渲染成一个
  **真实可解码的占位片段**（带移动标记、标记为 demo，不是 AI 生成）；没有可用 FFmpeg 时明确失败，
  绝不留假素材。旧库里已种下的 `demo-unsupported-v1` 会在启动时改名（用户自己改过的不动）；
- `configured` 不落库，是按环境变量实时派生的字段；
- 旧的 `provider_settings` 表保留为兼容镜像，但唯一事实来源是 `capability_settings`。

详见 [模型与任务](./06-model-providers-and-jobs.md) 第 15–17 节。

## 6. 质检模型

### QC Run

关联资产版本和质检策略版本。

### QC Result

```json
{
  "metric": "character_identity_similarity",
  "status": "warn",
  "score": 0.76,
  "threshold": 0.8,
  "evidence": [
    {"timecode_ticks": 52, "frame_asset_version_id": "av_frame_..."}
  ],
  "message": "面部相似度低于项目阈值"
}
```

结果采用 `pass/warn/fail`，不要用一个总分掩盖具体问题。

## 7. 时间线 IR

内部时间线与剪映、FFmpeg 解耦。

```json
{
  "schema_version": "1.0",
  "timebase": {"num": 1, "den": 25000},
  "canvas": {"width": 1920, "height": 1080, "fps_num": 25, "fps_den": 1},
  "tracks": [
    {
      "id": "v1",
      "kind": "video",
      "clips": [
        {
          "id": "clip_1",
          "asset_version_id": "av_video_1",
          "source_in": 0,
          "source_out": 100000,
          "timeline_in": 0,
          "timeline_out": 100000,
          "transform": {"scale": 1, "x": 0, "y": 0},
          "opacity": 1
        }
      ]
    }
  ]
}
```

Clip 必须引用 `asset_version_id`，不能只引用会变化的 `asset_id`。

建议轨道类型：`video`、`overlay`、`dialogue`、`sfx`、`music`、`subtitle`。

## 8. 成本账本

`cost_ledger` 只追加，不修改历史：

- estimate；
- reserve；
- settle；
- release；
- refund；
- manual_adjustment。

维度：项目、集、场、镜头、任务、attempt、Provider、模型、计量单位。

预算检查：

```text
已结算 + 已预留 + 本次最高可能费用 <= 硬上限
```

若不满足，则任务进入 `blocked_budget`，等待用户提高预算或改变策略。

## 9. 状态与过期标记

资产状态：

```text
draft → generated → reviewing → approved → locked
                         ↘ rejected
approved/locked → superseded → archived
```

当上游变化时，下游不删除，而是标记：

- `fresh`：依赖仍一致；
- `possibly_stale`：变化可能无影响；
- `stale`：已知输入版本变化；
- `orphaned`：原任务取消但文件仍存在。

## 10. 建议数据库表

```text
project, project_setting
story, story_version
season, episode, scene, shot, shot_version
dialogue_line, dialogue_version
bible, bible_version
character, character_version
location, location_version
prop, prop_version
style_guide, style_guide_version
voice_profile, pronunciation_entry
asset, asset_version, asset_relation
workflow_run, task_node, task_dependency, task_attempt, task_event
qc_policy, qc_run, qc_result, review_decision
timeline, timeline_version, track, clip, transition, caption
provider_account, model_catalog, model_capability
cost_ledger, budget
export_job, export_artifact
audit_log
```

MVP 可以合并若干版本表，但必须保留“逻辑实体”和“不可变具体版本”的区分。
