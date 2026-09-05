# 会锚 DWS 项目资料同步

仅在仓库根目录执行本任务。固定私有任务配置路径为
`.private/qwenwork-dws-project-sync.json`；不要接受其他配置路径、命令文本或附加参数。

## 私有任务配置

读取任何内容前，先对固定路径调用 `Path.lstat`：路径必须是现有普通文件，拒绝 symlink、
Windows reparse point、目录、named pipe 和其他特殊文件，且 lstat 大小不得超过 65536
bytes。随后只打开一次二进制只读流并调用一次 `read(65537)`；返回超过 65536 bytes 时立即
停止。只有通过这些门禁后才执行 UTF-8 解码和 JSON 解析，并按以下 JSON Schema 严格验证。
配置必须是 JSON object，`schema_version` 必须为 `1`，七个字段必须全部存在且不得有额外
字段。五个路径字段必须是 E 盘绝对路径；配置不得包含 profile、token、gateway、任意
argv 或业务正文。

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
    "dws": {"type": "string", "pattern": "^[Ee]:\\\\"},
    "source_bundle": {"type": "string", "pattern": "^[Ee]:\\\\"},
    "context_artifact": {"type": "string", "pattern": "^[Ee]:\\\\"},
    "state": {"type": "string", "pattern": "^[Ee]:\\\\"}
  }
}
```

配置缺失、不可读、schema 不匹配、路径不绝对或路径不在 E 盘时立即停止，不得尝试默认值。
schema 验证后，必须用 `Path.resolve(strict=False)` 和 `os.path.normcase` 规范化路径。
五个配置路径与固定任务配置路径必须两两不同；对已存在的任意路径对再用 `os.path.samefile`
确认不是同一文件。任何 symlink、hardlink 或 Windows reparse 目标无法确认、指向同一文件
或可能让输出覆盖输入时立即停止。`manifest` 和 `dws` 必须是现有普通文件，三个输出路径的
父目录必须是现有目录。

`hui-anchor-dws-project-context-v1` 是必须预先安装的外部 Skill 依赖。在执行 collect 前，
只通过 Skill 注册表检查该精确名称；不可用时立即停止。不得搜索、安装或替换 Skill，也
不得把相近名称、仓库文件或通用模型提示当作降级实现。

## 固定流程

1. 使用参数数组在仓库根目录运行 `python -m tools.dws_project_sync collect`。参数映射固定为：
   `--manifest` 取 `manifest`，`--project` 取 `project`，`--dws-path` 取 `dws`，
   `--output` 取 `source_bundle`。不得拼接 shell 命令或增加任何参数。
2. collect 成功后，调用名称精确为 `hui-anchor-dws-project-context-v1` 的 Skill。该 Skill 的
   唯一输入是从 `source_bundle` 读取并校验后的 `DwsSourceBundle`；不得传入历史对话、
   仓库文档、其他私有文件或模型记忆。唯一输出是一个
   `QwenProjectContextArtifact` JSON object，顶层只允许 `schema_version`、`context` 和
   `completed_retrieval_request_ids`。
3. artifact 中每条事实性决策必须使用 `DecisionCard.source_refs`；每条行动项、风险和下次
   会议必须分别使用 `sourced_actions`、`sourced_risks` 和
   `sourced_next_meeting` 的 `SourcedFact(text, source_refs)`。每个引用必须精确匹配一个
   `active` 来源的类型、ID、权限域、标题、URL 和时间，且 excerpt 必须是来源正文中的
   精确片段。事实文本必须由这些摘录直接支持；证据不足就省略，不得推断或补写。
4. legacy 展示字段必须固定为空，不得从 sourced facts 复制或生成：

   ```json
   {
     "open_actions": [],
     "current_risks": [],
     "next_meeting": null
   }
   ```

5. 当前 `DwsSourceBundle` 不包含待检索请求输入，因此
   `completed_retrieval_request_ids` 必须为 `[]`。未完成的检索请求绝不能加入
   `completed_retrieval_request_ids`，遗漏的请求保持 pending。不得读取检索接口或实现
   额外 fetch。只有已取得对应证据且请求 ID 明确包含在唯一输入中，才允许记录完成；
   当前输入契约不满足该条件。
6. 写文件前先在内存中用 `QwenProjectContextArtifact.model_validate` 完整验证 candidate，
   再编码为 canonical UTF-8 JSON；encoded bytes 必须小于或等于 `2097152`。模型或大小
   检查失败时保留旧目标，且不得创建或打开任何输出文件。两项检查通过后才
   创建同目录临时文件。写入后依次 `flush`、`fsync`，再用 `os.replace` 替换
   `context_artifact`。失败时
   删除临时文件并保留旧目标；不得直接覆盖目标。
7. artifact 写入成功后，使用参数数组运行 `python -m tools.dws_project_sync push`。参数映射
   固定为：`--manifest` 取 `manifest`，`--project` 取 `project`，`--sources-file` 取
   `source_bundle`，`--context-file` 取 `context_artifact`，`--state-file` 取 `state`；
   `--gateway` 固定为 `http://127.0.0.1:8731`。令牌只由
   `COMPANION_DWS_SYNC_TOKEN` 提供。

## 读取、写入和输出边界

- 任务编排只可读取固定任务配置和 `source_bundle`。Skill 不得读取其他文件、网络资源、
  历史对话或 manifest 白名单以外的钉钉资料。collect/push 只能通过固定 CLI 完成其契约内
  的 manifest、DWS、context 和 state 访问。
- 任务只可写 `source_bundle`、`context_artifact` 和 `state`。不得创建日志、报告、缓存、
  旁路 artifact 或额外状态文件。
- 任一步失败即停止。不得绕过校验、拆分超限同步包、改用其他 gateway、追加 `--yes`、
  重试非 retryable 错误或继续 push。
- 不得输出其他内容。用户可见输出必须原样保留当前步骤 CLI 返回的单个脱敏 JSON 状态
  object。禁止输出配置、命令、profile、资源 ID、来源标题或 URL、正文、摘录、私有路径、
  token、sync ID 或 generation ID。
