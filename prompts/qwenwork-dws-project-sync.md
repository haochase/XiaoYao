# 会锚 DWS 项目资料同步

仅在仓库根目录执行本任务。固定私有任务配置路径为
`.private/qwenwork-dws-project-sync.json`；不要接受其他配置路径、命令文本或附加参数。

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
立即停止，不得尝试默认值。
schema 验证后，必须用 `Path.resolve(strict=False)` 和 `os.path.normcase` 规范化路径。
五个配置路径与固定任务配置路径必须两两不同；对已存在的任意路径对再用 `os.path.samefile`
确认不是同一文件。任何 symlink、hardlink 或 Windows reparse 目标无法确认、指向同一文件
或可能让输出覆盖输入时立即停止。`manifest` 和 `dws` 必须是现有普通文件，三个输出路径的
父目录必须是现有目录。

`dws` 若指向官方 wrapper，必须通过固定安装位置、精确 wrapper 结构、当前受支持 Windows
架构及同目录现有普通非 reparse 原生 shim 的启动校验；不得复制或修改安装文件。不得把任意
脚本作为 DWS 启动入口。

`hui-anchor-dws-project-context-v1` 是必须预先安装的外部 Skill 依赖。在执行 collect 前，
只通过 Skill 注册表检查该精确名称；不可用时立即停止。不得搜索、安装或替换 Skill，也
不得把相近名称、仓库文件或通用模型提示当作降级实现。

## 固定流程

生产入口使用 `python tools/dws_sync_runtime.py`，解释器须为已安装本项目依赖的明确绝对路径。
该入口只读固定配置，以 CurrentUser DPAPI 解封本地凭据，仅向 pending/push 的内部调用
提供网关 token，不修改父进程环境、不把 token 传给 DWS 采集子进程。不得手工导出凭据。
先执行 `python tools/dws_sync_runtime.py check`；configured 只证明配置/解密正常，仍需上面的
Skill 注册表和真实会话检查，不等同于同步已运行。prepare/serve 不属于周期任务，不自动执行。

1. 使用参数数组运行 `python tools/dws_sync_runtime.py begin`，项目只取固定配置。
   若返回 `coalesced`，本次触发立即正常结束，不执行任何后续步骤；
   若返回 `started`，只在本次任务内部保存 `run_token`，不得向用户输出或写入其他文件。
   从此步开始进入 `try/finally`：只有 `end` 成功才算正常释放；其他任何失败、中断或取消均须
   在 `finally` 中以同一 token 调用 `python tools/dws_sync_runtime.py abort --run-token TOKEN`。
2. 使用参数数组在仓库根目录运行 `python tools/dws_sync_runtime.py collect`，仅传入
   `--run-token`，取 begin 返回的 token。其余参数由固定配置映射。不得拼接 shell 命令。
3. collect 成功后，使用参数数组运行 `python tools/dws_sync_runtime.py pending`，只传同一
   `--run-token`。网关固定为 `http://127.0.0.1:8731`，内部只向该请求提供
   `COMPANION_DWS_SYNC_TOKEN`。该命令只领取状态为
   pending 的项目内请求，将每个
   `source_id_hash` 映射为 manifest 白名单内的 `source_type` 和 `source_id`，并把只含
   `request_id`、`query_hash`、`request_epoch`、`attempt_count`、`lease_expires_at`、
   `lease_token`、`sources` 的 `retrieval_requests` 原子写回 source bundle。
   任一 hash 无法唯一映射时立即停止，不得扩大白名单。
4. pending 成功后，调用名称精确为 `hui-anchor-dws-project-context-v1` 的 Skill。该 Skill 的
   唯一输入是从 `source_bundle` 读取并校验后的 `DwsSourceBundle`；不得传入历史对话、
   仓库文档、其他私有文件或模型记忆。唯一输出是一个
   `QwenProjectContextArtifact` JSON object，顶层只允许 `schema_version`、`context` 和
   `completed_retrieval_request_ids`。
5. artifact 中每条事实性决策必须使用 `DecisionCard.source_refs`；每条行动项、风险和下次
   会议必须分别使用 `sourced_actions`、`sourced_risks` 和
   `sourced_next_meeting` 的 `SourcedFact(text, source_refs)`。每个引用必须精确匹配一个
   `active` 来源的类型、ID、权限域、标题、URL 和时间，且 excerpt 必须是来源正文中的
   精确片段。事实文本必须由这些摘录直接支持；证据不足就省略，不得推断或补写。
6. legacy 展示字段必须固定为空，不得从 sourced facts 复制或生成：

   ```json
   {
     "open_actions": [],
     "current_risks": [],
     "next_meeting": null
   }
   ```

7. 本次安装的 v1 Skill 缺少问题原文与检索基线，`completed_retrieval_request_ids` 固定为空，
   不得仅凭 query_hash 或 active 来源猜测已经完成。下述是协议能力的必要条件而不是自动授权：
   只有已取得对应证据，且请求 ID 在 `retrieval_requests` 中列出、其 `sources` 全部在
   本轮成功取得 active 证据、事实由对应精确摘录支持时，才能加入
   `completed_retrieval_request_ids`。
   未完成的检索请求绝不能加入 `completed_retrieval_request_ids`，遗漏的请求保持
   pending。不得
   捏造请求、修改 `query_hash`、扩大 `sources` 或用旧 bundle 中不存在的 ID 声明完成。
   CLI 会把这些 ID 精确映射为本轮 claim 的 `completed_retrieval_claims`，其中只包含
   `request_id`、`request_epoch`、`attempt_count` 和 `lease_token`；不得自行构造或修改 claim。
8. 不得直接写 `context_artifact`。先在内存中用
   `QwenProjectContextArtifact.model_validate` 完整验证 Skill candidate，再编码为 canonical
   UTF-8 JSON；encoded bytes 必须小于或等于 `2097152`。然后使用参数数组运行
   `python tools/dws_sync_runtime.py artifact`，仅传同一 `--run-token`，
   并仅通过 stdin 传入 encoded bytes。该命令在 token fencing 下
   创建同目录临时文件，依次 `flush`、`fsync`、`os.replace`；验证或写入失败时保留旧目标。
9. artifact 写入成功后，使用参数数组运行 `python tools/dws_sync_runtime.py push`，只传同一
   `--run-token`。首次人工验收先加 `--dry-run`，预检通过后再实际 push；正常周期不重复 dry-run。
10. push 成功后，以同一 token 运行 `python tools/dws_sync_runtime.py end --run-token TOKEN`。
    返回 `completed`
    时结束；返回 `rerun` 时只使用返回的新 token 再执行一次完整的 collect -> pending ->
    Skill -> artifact -> push -> end 链路。任何未成功 end 的路径都必须由 `finally` 调用 abort；
    旧 token 不得再写 bundle、artifact、state 或发起 push。

## 读取、写入和输出边界

- 任务编排只可读取固定任务配置和 `source_bundle`。Skill 不得读取其他文件、网络资源、
  历史对话或 manifest 白名单以外的钉钉资料。collect/pending/push 只能通过固定 CLI
  完成其契约内的 manifest、DWS、retrieval、context 和 state 访问。
- 任务只可写 `source_bundle`、`context_artifact`、`state`，以及 CLI 在仓库固定
  `.private/dws-sync-locks` 下管理的哈希命名 lifecycle/lock 文件。不得创建日志、报告、
  缓存、旁路 artifact 或其他状态文件。
- runtime 仅可读取 prepare 生成的 `.private/dws-runtime/credential.dpapi`；本任务不得创建、
  修改、打印或复制它。不得读取其他 Windows 用户或其他应用的凭据。
- 任一步失败即停止。不得绕过校验、拆分超限同步包、改用其他 gateway、追加 `--yes`、
  重试非 retryable 错误或继续 push。
- 不得输出其他内容。begin/end 返回的 `run_token` 只供任务内部编排，绝不能成为用户可见
  输出。用户可见输出必须原样保留最终步骤 CLI 返回的单个脱敏 JSON 状态
  object。禁止输出配置、命令、profile、资源 ID、来源标题或 URL、正文、摘录、私有路径、
  token、sync ID 或 generation ID。
