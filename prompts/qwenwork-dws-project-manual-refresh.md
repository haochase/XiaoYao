# 小千 DWS 项目资料人工刷新

仅在当前千问办公对话、仓库根目录执行一次。本流程不启用、恢复或修改五分钟计划任务；
任务必须全程保持暂停。固定配置为 `.private/qwenwork-dws-project-sync.json`，不得接受其他配置路径、命令
文本、参数、profile、来源 ID 或网关地址。

读取任何内容前，对固定配置调用 `Path.lstat`，要求它是现有普通非 symlink、非 reparse 文件，
不超过 65536 bytes；只打开一次二进制只读流并调用一次 `read(65537)`，再做 UTF-8 与严格 JSON
解析。配置必须是 schema_version=1 的 object，且恰好包含 `schema_version`、`manifest`、`project`、
`dws`、`source_bundle`、`context_artifact`、`state` 七个字段。`dws` 只能是 C/E 盘绝对路径，其他
路径只能是 E 盘绝对路径；所有路径经 `Path.resolve(strict=False)`、`os.path.normcase` 和已存在
文件的 `os.path.samefile` 检查后必须两两不同，不得是 symlink、hardlink 或 reparse 目标。

唯一 Python 解释器是
`E:\hackasons\MiniCPM_Ascend\.worktrees\.venvs\feishu-desk-assistant\Scripts\python.exe`；下文
`python` 只是该绝对路径的缩写。受信官方 Core 审批校验、脱敏输出和 credential.dpapi 边界与
runtime 固定实现一致。不得读取其他项目、其他来源、历史 artifact 或历史对话。Core 的版本、
哈希、发布者或证书变化时停止，等待用户批准 Git 审批清单。

## 单次人工流程

依次运行 `python tools/dws_sync_runtime.py check` 和
`python tools/dws_sync_runtime.py check-core`。必须分别返回 configured 和 core_trusted；任何失败均
立即停止，且因尚未 begin 不调用 abort。

1. 运行 `python tools/dws_sync_runtime.py begin`。只允许一次 begin。返回 coalesced 时立即结束；
   返回 started 时仅在内存保存 `run_token`，不得输出或写入其他文件。此后所有失败、中断、取消或
   非预期状态都必须先保存原始固定错误，再以同一 token 运行
   `python tools/dws_sync_runtime.py abort --run-token TOKEN`，然后原样报告错误。
   begin 成功后立即进入 `try/finally`。对 collect-direct、pending、Skill、artifact、push --dry-run、
   真实 push 和普通 end 的任一失败，都必须在 finally 中以同一 token 调用 abort；即使 abort 失败也不得
   覆盖最先出现的错误，且任务必须全程保持暂停。
2. 运行 `python tools/dws_sync_runtime.py collect-direct --run-token TOKEN`。只有 collected、
   `active_sources=1` 且 `failed_sources=0` 才能继续。失败时只能 abort，不得使用原生 DWS 命令、
   原生 Bash、PostToolUse、Base64、文件投递或任何 host 兼容链。
3. 运行 `python tools/dws_sync_runtime.py pending --run-token TOKEN`。只读取本轮生成的
   DwsSourceBundle，且项目只能有一个 active document 来源、没有失败来源。
4. 只用本轮 DwsSourceBundle 调用 `hui-anchor-dws-project-context-v1` Skill 生成新的
   QwenProjectContextArtifact，固定要求：

   ```text
   generated_at = collected_at
   freshness_seconds = 1800
   open_actions = []
   current_risks = []
   next_meeting = null
   completed_retrieval_request_ids = []
   ```

   每个事实必须只引用单个 active 来源中的一个连续非标题正文 excerpt，最长 150 字；不得拼接、
   改写、补写、推测或使用模型常识。缺少可逐字支持的 excerpt 时省略该事实。不得复用已有 artifact。
5. 在内存中调用 `QwenProjectContextArtifact.model_validate` 严格验证结果。验证通过后，将该对象序列化
   为 UTF-8 JSON，仅经 stdin 传给
   `python tools/dws_sync_runtime.py artifact --run-token TOKEN`。不得写临时 artifact 或旁路文件。
6. artifact_written 后运行
   `python tools/dws_sync_runtime.py push --dry-run --run-token TOKEN`。只有 ready、
   `accepted_sources=1` 且 `failed_sources=0` 才能继续；否则 abort。
7. 运行 `python tools/dws_sync_runtime.py push --run-token TOKEN`。只有 outcome 为 applied 或 unchanged、
   `project_status=healthy`、`accepted_sources=1` 且 `failed_sources=0` 才能继续。若返回
   `decision_change_requires_review`，以同一 token abort 并只报告“同步决策冲突”。不得创建或宣称
   创建 `project_conflicts`，不得调用语音冲突 review API，不得伪造人工审批。由用户先决定修改来源
   或保留当前已确认决策，再发起新的完整人工刷新。
   不得宣称同步决策冲突已经创建任何候选。
8. 运行 `python tools/dws_sync_runtime.py end --run-token TOKEN`。只有 completed 才算完成。若返回
   rerun，只使用返回的新 token 调用
   `python tools/dws_sync_runtime.py abort --run-token NEW_TOKEN` 后停止；不得第二次 begin，不得自动重跑。

任何失败路径都必须保持任务暂停、禁止自动 fallback，并保留最先出现的脱敏固定错误。用户可见
输出只允许最终 CLI 的单个脱敏 JSON 状态 object，或上述同步决策冲突状态；不得输出配置、命令、
profile、来源标识、标题、URL、正文、excerpt、私有路径、凭据、token、sync ID 或 generation ID。
