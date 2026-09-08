# 小千 DWS 项目资料无人值守同步

仅在仓库根目录执行本任务。固定私有任务配置路径为
`.private/qwenwork-dws-project-sync.json`；不要接受其他配置路径、命令文本或附加参数。
本 prompt 不启用、恢复或修改外部计划任务；五分钟千问办公任务必须保持暂停，直到用户另行批准。

## 私有任务配置

读取任何内容前，先对固定路径调用 `Path.lstat`：路径必须是现有普通文件，拒绝 symlink、
Windows reparse point、目录、named pipe 和其他特殊文件，且 lstat 大小不得超过 65536
bytes。随后只打开一次二进制只读流并调用一次 `read(65537)`；返回超过 65536 bytes 时立即
停止。只有通过这些门禁后才执行 UTF-8 解码和 JSON 解析，并按以下 JSON Schema 严格验证。
配置必须是 JSON object，`schema_version` 必须为 `1`，七个字段必须全部存在且不得有额外
字段。`dws` 必须是 C 或 E 盘绝对本地文件路径；其他四个路径字段必须是 E 盘绝对路径；
配置不得包含 profile、token、gateway、任意 argv、任意命令文本或业务正文。

<!-- task-config-schema -->
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version",
    "manifest",
    "project",
    "dws",
    "source_bundle",
    "context_artifact",
    "state"
  ],
  "properties": {
    "schema_version": {"const": 1},
    "manifest": {"type": "string", "pattern": "^[Ee]:\\\\"},
    "project": {
      "type": "string",
      "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    },
    "dws": {"type": "string", "pattern": "^[CcEe]:\\\\"},
    "source_bundle": {"type": "string", "pattern": "^[Ee]:\\\\"},
    "context_artifact": {"type": "string", "pattern": "^[Ee]:\\\\"},
    "state": {"type": "string", "pattern": "^[Ee]:\\\\"}
  }
}
```

配置缺失、不可读、schema 不匹配、路径不绝对、`dws` 不在 C/E 盘或其他路径不在 E 盘时
立即停止，不得尝试默认值。schema 验证后，必须用 `Path.resolve(strict=False)` 和
`os.path.normcase` 规范化路径。五个配置路径与固定任务配置路径必须两两不同；对已存在的任意
路径对再用 `os.path.samefile` 确认不是同一文件。任何 symlink、hardlink 或 Windows reparse
目标无法确认、指向同一文件或可能让输出覆盖输入时立即停止。`manifest` 和 `dws` 必须是现有
普通文件，三个输出路径的父目录必须是现有目录。

配置中的dws绝对路径必须指向官方 wrapper，并由 runtime 验证固定安装位置、wrapper、原生 shim、受信官方
Core、Git 审批清单、版本、SHA-256、发布者和签名证书。任一审批值变化必须停止并由用户批准
Git 审批清单变更；不得复制、修改或替换安装文件，不得以脚本作为 DWS 入口。

## 固定运行边界

本任务唯一允许的 Python 解释器为
`E:\hackasons\MiniCPM_Ascend\.worktrees\.venvs\feishu-desk-assistant\Scripts\python.exe`。
下文命令中的 `python` 只是该绝对路径的排版缩写；实际每个 Python runtime 工具调用的
argv[0] 必须直接使用此绝对路径。不得搜索或枚举其他 Python 解释器，不得检查实现源码或测试
文件，不得使用 `cd &&` 或任何 shell 命令串联，不得创建辅助脚本、候选文件或旁路产物。

先分别使用参数数组运行：

`python tools/dws_sync_runtime.py check`

`python tools/dws_sync_runtime.py check-core`

`["E:\\hackasons\\MiniCPM_Ascend\\.worktrees\\.venvs\\feishu-desk-assistant\\Scripts\\python.exe","tools/dws_sync_runtime.py","check"]`

`["E:\\hackasons\\MiniCPM_Ascend\\.worktrees\\.venvs\\feishu-desk-assistant\\Scripts\\python.exe","tools/dws_sync_runtime.py","check-core"]`

`check` 必须返回 configured，`check-core` 必须返回 core_trusted。begin前的预检、check或
check-core失败时输出脱敏固定错误并立即结束，不调用abort。只有 begin 成功取得 run_token 后，
任一固定命令失败才走 abort 分支。

生产入口只读固定配置。`collect-direct` 在 runtime 内直接启动每次重新校验的官方 Core；Core
子进程使用固定参数数组、最小环境和有界输出。`pending` 与 `push` 才可在内部读取
`.private/dws-runtime/credential.dpapi` 并把 token 仅交给回环网关。不得手工导出凭据。

direct 采集失败只允许以同一 token 调用 abort。不得回退到原生 Bash、PostToolUse、stdin 正文、
Base64、文件投递或旧 host-import 兼容链；不得调用模型，不得自行构造 source bundle。

单个调度触发最多一次 begin。取得 `run_token` 后，任何命令非成功，都必须先在内存中保存该失败
命令返回的固定错误，再以同一 token 调用 abort；随后原样输出固定错误并 return。即使 abort
自身失败，也不得用其结果覆盖原错误。abort 后不得 begin。只有 end=rerun 才允许使用 end 返回的
新 token 完整重跑。每轮必须重新执行 collect-direct，禁止 Agent 直接读取或手工回放
context_artifact；仅允许受信 runtime 通过 reuse-artifact 执行 approved artifact 复用门禁。
确定性 push 错误不得再次 push。

## 唯一无人值守流程

1. 使用参数数组运行 `python tools/dws_sync_runtime.py begin`。若返回 `coalesced`，立即正常结束；
   若返回 `started`，只在内存中保存 `run_token`，不得输出或写入其他文件。从此步进入
   `try/finally`，只有成功 end 才正常释放，其他失败、中断或取消都以同一 token 调用
   `python tools/dws_sync_runtime.py abort --run-token TOKEN`。
2. 运行 `python tools/dws_sync_runtime.py collect-direct --run-token TOKEN`。只有返回 `collected`，
   且 `active_sources=1`、`failed_sources=0` 才能继续。
3. 运行 `python tools/dws_sync_runtime.py pending --run-token TOKEN`。它只领取项目内
   `retrieval_requests`，并将其映射到 manifest 内唯一来源；无法唯一映射时 abort。
4. 运行 `python tools/dws_sync_runtime.py reuse-artifact --unattended --run-token TOKEN`。只有返回
   `artifact_reused` 才能继续 push。若返回 `manual_refresh_required`，保存该状态，以同一 token
   运行 `python tools/dws_sync_runtime.py abort --run-token TOKEN`，原样输出状态并 return。此分支
   不得调用模型、不得写 context artifact、不得 push、不得 dry-run。
5. 运行 `python tools/dws_sync_runtime.py push --run-token TOKEN`。不得添加 dry-run，不得重试确定性
   错误。若返回 `decision_change_requires_review`，以同一 token abort，只报告同步决策冲突；不得
   创建或宣称创建 `project_conflicts`，不得调用语音冲突审核接口。
6. push 成功后，以同一 token 运行 `python tools/dws_sync_runtime.py end --run-token TOKEN`。返回
   `completed` 时结束；返回 `rerun` 时只使用返回的新 token，依次完整重做 collect-direct -> pending -> reuse-artifact --unattended -> push -> end。
   rerun 中任一步失败也必须以 rerun token abort。任何未成功 end 的路径都必须由 finally 调用
   abort；旧 token 不得再写 bundle、context artifact、state 或发起 push。

## 读取、写入和输出边界

- 任务编排只可读取固定任务配置、其中指定的 manifest 和 source bundle。不得读取其他文件、网络
  资源、历史对话或白名单之外的资料。
- 只能通过受信 runtime CLI 写 source bundle、context artifact、state 和固定私有 lifecycle/lock
  文件。不得创建日志、报告、缓存或旁路 artifact。
- 任一步失败即停止。不得绕过校验、扩大白名单、改用其他 gateway、追加 `--yes` 或自动 fallback。
- 不得输出其他内容。run_token 绝不能成为用户可见输出；用户可见输出只能是最终步骤返回的单个
  脱敏 JSON 状态 object。禁止输出配置、命令、profile、资源 ID、来源标题或 URL、正文、摘录、
  私有路径、token、sync ID 或 generation ID。
