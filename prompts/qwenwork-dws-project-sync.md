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

## 固定流程

本任务唯一允许的Python解释器为
`E:\hackasons\MiniCPM_Ascend\.worktrees\.venvs\feishu-desk-assistant\Scripts\python.exe`。
下文命令中的`python`只是该绝对路径的排版缩写；实际每个Python runtime工具调用的argv[0]
必须直接使用此绝对路径。两次DWS原生命令必须以固定字面量`dws`作为argv[0]；必须使用
PATH-based字面命令`dws`，不得添加引号、路径或前缀。配置中的`dws`绝对路径只用于本地可信入口
校验，不得作为千问办公宿主Bash调用的argv[0]。`check`的参数数组必须等价于：
`["E:\\hackasons\\MiniCPM_Ascend\\.worktrees\\.venvs\\feishu-desk-assistant\\Scripts\\python.exe","tools/dws_sync_runtime.py","check"]`。
不得搜索或枚举其他Python解释器，不得检查实现源码或测试文件，不得使用`cd &&`或
任何shell命令串联，不得创建辅助脚本、候选文件或旁路产物。任务工作目录已由contextDirs固定为
仓库根目录，不得再次切换目录。除读取固定配置、manifest、source_bundle以及执行本节明确命令外，
允许校验DWS wrapper和原生shim；除此之外不得做解释器发现、代码探查
或额外诊断。begin前的预检或check失败时输出脱敏固定错误并立即结束，不调用abort；只有begin成功
取得run_token后，任一固定命令失败才按abort分支结束。

生产入口使用 `python tools/dws_sync_runtime.py`，解释器须为已安装本项目依赖的明确绝对路径。
该入口只读固定配置；`capture-info`、`complete-host-import` 和 `reuse-artifact` 不读取或解封凭据，
只有 pending/push 的内部调用以
CurrentUser DPAPI 解封并取得网关 token。runtime 不修改父进程环境，也不把 token 传给
DWS。不得手工导出凭据。
两次字面命令`dws`必须由千问办公原生Bash的宿主PostToolUse路由执行；这里的`dws`是平台托管
命令令牌，不把guest shell的PATH文件身份作为信任依据。每次调用只接受宿主返回的真实
`dws_tool_result`通道结果，再由同一次调用的`--jq`生成封包；普通shell stdout、历史结果、
`pending-post-tool-use`占位符或任何非`dws_tool_result`来源都必须abort并结束。配置中的绝对shim
及`resolve_dws_launch`仅用于本地runtime边界验证，不能替代或冒充宿主托管命令令牌。
先执行 `python tools/dws_sync_runtime.py check`；configured 只证明配置/解密正常，仍需真实会话检查，
不等同于同步已运行。prepare/serve 不属于周期任务，不自动执行。

单个调度触发最多一次 `begin`。取得 `run_token` 后，任何命令非成功，都必须先在内存中保存该失败
命令返回的固定错误，再以同一 token 调用 `abort`；随后原样输出固定错误并 `return`。即使
`abort` 自身失败，也不得用其结果覆盖原错误。
`abort` 后不得 `begin`。只有 `end=rerun` 才允许使用 `end` 返回的新 token 完整重跑。每轮必须重新
执行两次 DWS 和两阶段导入，仍通过无人值守复用门禁；禁止读取或回放 `context_artifact`。确定性
push 错误不得再次 push。

1. 使用参数数组运行 `python tools/dws_sync_runtime.py begin`，项目只取固定配置。
   若返回 `coalesced`，本次触发立即正常结束，不执行任何后续步骤；
   若返回 `started`，只在本次任务内部保存 `run_token`，不得向用户输出或写入其他文件。
   从此步开始进入 `try/finally`：只有 `end` 成功才算正常释放；其他任何失败、中断或取消均须
   在 `finally` 中以同一 token 调用 `python tools/dws_sync_runtime.py abort --run-token TOKEN`。
2. 从固定配置定位 manifest，按与配置相同的严格文件门禁读取，不回显配置或 manifest。
   只取项目键精确匹配且来源恰好为一个 document 的项目，并只在任务内保存它的 profile 和
   source ID。进行第一个独立工具调用：使用千问办公原生 Bash 执行固定 DWS `doc info` 命令。
   同一次调用的宿主 PostToolUse 必须只从真实 `dws_tool_result` 运行以下 jq，让结果直接包含固定
   `operation=doc_info`，并把 Base64 切成每段最多 64 个字符，避免宿主 `content` 通道在长字符串中
   插入换行；不得由 Agent 补写任何键：

   ```bash
   dws doc info --profile '<PROFILE_LITERAL>' --format json --node '<SOURCE_ID_LITERAL>' --jq 'tojson as $raw | ($raw|@base64) as $b | {operation:"doc_info",encoding:"base64-json",byte_count:($raw|utf8bytelength),payload_chunks:[range(0;($b|length);64) as $i|$b[$i:$i+64]]}'
   ```

   发起调用前必须逐项确认 `--profile`、`--format json`、`--node`、`--jq` 四个参数。
   `*_LITERAL` 必须整体替换为本次内存中已验证的值，并按 POSIX 单引号规则编码为恰好一个 Bash
   参数；不得依赖 shell 环境变量，残留任何 `<LITERAL>` 占位符时不得执行。结果为
   `pending-post-tool-use` 占位符、缺失、超限或 jq 失败时立即停止。
3. 进行下一次独立 CLI 工具调用：运行
   `python tools/dws_sync_runtime.py capture-info --run-token TOKEN`。步骤 2 的宿主结果根对象必须严格为
   `type/content` 两个键且 `type=dws_tool_result`；只取 `content` 字符串值，在内存中严格解析为键集合
   `operation/encoding/byte_count/payload_chunks` 的 JSON object，再将该 object 序列化为 UTF-8 JSON
   交给 stdin。不得把 `type/content` 外层包装交给 stdin，不得使用普通 stdout、历史结果或 pending
   placeholder，不得手工复制或拼接 Base64 分片，不得写临时文件，也不得增加其他字段。成功状态必须为
   `host_info_captured`。
4. 进行第三个独立工具调用：以相同 dws、profile、JSON 格式和 source ID 执行固定 DWS
   `doc read`。同一次宿主 jq 直接加入固定 `operation=doc_read`：

   ```bash
   dws doc read --profile '<PROFILE_LITERAL>' --format json --node '<SOURCE_ID_LITERAL>' --jq 'tojson as $raw | ($raw|@base64) as $b | {operation:"doc_read",encoding:"base64-json",byte_count:($raw|utf8bytelength),payload_chunks:[range(0;($b|length);64) as $i|$b[$i:$i+64]]}'
   ```

   两次 DWS 调用不得合并。再次逐项确认四个参数；不得使用管道，不得使用命令替换，不得使用
   Popen、重定向或临时文件。结果只接受同一次真实 `dws_tool_result` 通道生成的完整 envelope。
5. 进行下一次独立 CLI 工具调用：运行
   `python tools/dws_sync_runtime.py complete-host-import --run-token TOKEN`，只把步骤 4 同一次原生 Bash
   返回的 `dws_tool_result` 按步骤 3 的相同规则只取并严格解析 `content`，再把内层 envelope 交给
   stdin。不得把 `type/content` 外层包装交给 stdin，不得由 Agent 增加 operation、合并两次结果、
   构造外层 object 或手工复制、拼接 Base64 分片。成功状态必须为 `collected`。complete 事务失败会先恢复 capture、output 和
   `host_info`，随后 finally 使用同一 token abort，abort 随后按设计清理 capture；
   不承诺 abort 后保留诊断文件，也不得为了诊断跳过 abort 或另存 capture。
6. 两阶段导入成功后，使用参数数组运行 `python tools/dws_sync_runtime.py pending --run-token TOKEN`。
   网关固定为 `http://127.0.0.1:8731`，内部只向该请求提供 `COMPANION_DWS_SYNC_TOKEN`。该命令只领取
   pending 的项目内请求，将每个 `source_id_hash` 映射为 manifest 白名单内的 `source_type` 和
   `source_id`，并把固定 retrieval request 字段原子写回 source bundle。任一 hash 无法唯一映射时
   立即停止，不得扩大白名单。
7. pending 成功后，使用参数数组运行
   `python tools/dws_sync_runtime.py reuse-artifact --unattended --run-token TOKEN`。只有返回
   `artifact_reused` 才能继续 push。若返回 `manual_refresh_required`，先在内存保存该固定状态，立即
   使用同一 token 运行 `python tools/dws_sync_runtime.py abort --run-token TOKEN`，随后原样输出
   `manual_refresh_required` 并 `return`；即使 abort 失败，也不得覆盖原状态。此分支不得调用模型、
   不得写 context artifact、不得 push、不得 dry-run，也不得第二次 begin。其他状态或错误走相同的
   fail-closed abort/return 边界。来源变化、approved artifact 缺失或存在 `retrieval_requests` 时均由
   CLI 返回该人工刷新状态，不得在正常周期自行处理。
8. artifact_reused 后，使用参数数组运行 `python tools/dws_sync_runtime.py push --run-token TOKEN`。
   正常周期不得添加 dry-run；确定性 push 错误不得重试。
9. push 成功后，以同一 token 运行 `python tools/dws_sync_runtime.py end --run-token TOKEN`。返回
   `completed` 时结束；返回 `rerun` 时只使用返回的新 token，依次完整重做 dws doc info ->
   capture-info -> dws doc read -> complete-host-import -> pending -> reuse-artifact --unattended -> push ->
   end。rerun 的 `manual_refresh_required` 同样必须以 rerun token abort 并 return。任何未成功 end 的路径
   都必须由 finally 调用 abort；旧 token 不得再写 bundle、context artifact、state 或发起 push。

## 读取、写入和输出边界

- 任务编排只可读取固定任务配置、其中指定的 manifest 和 `source_bundle`。不得读取其他文件、网络
  资源、历史对话或 manifest 白名单以外的钉钉资料。DWS 只允许上述两个独立宿主调用；两阶段导入、
  pending、复用门禁和 push 只能通过固定 CLI 完成。
- 任务编排只能通过受信 CLI 写 `source_bundle`、`context_artifact`、`state`，以及仓库固定
  `.private/dws-sync-locks` 下的哈希命名 lifecycle/lock 文件。只有受信 CLI 可以管理固定
  `.private/dws-host-captures` 下 digest 命名的 capture 文件及相关锁。Agent 不得直接读取 capture，
  Agent 不得直接复制 capture，Agent 不得直接修改 capture，Agent 不得直接另存 capture，
  Agent 不得直接删除 capture；也不得创建日志、报告、缓存、旁路 artifact 或其他状态文件。
- runtime 仅可在 pending/push/check 需要时读取 prepare 生成的
  `.private/dws-runtime/credential.dpapi`；capture-info、complete-host-import 和 reuse-artifact 不得读取或
  解密它。本任务不得创建、修改、打印或复制它。不得读取其他 Windows 用户或其他应用的凭据。
- 任一步失败即停止。不得绕过校验、拆分超限同步包、改用其他 gateway、追加 `--yes`、
  重试非 retryable 错误或继续 push。
- 不得输出其他内容。begin/end 返回的 `run_token` 只供任务内部编排，绝不能成为用户可见
  输出。用户可见输出必须原样保留最终步骤 CLI 返回的单个脱敏 JSON 状态
  object。禁止输出配置、命令、profile、资源 ID、来源标题或 URL、正文、摘录、私有路径、
  token、sync ID 或 generation ID。
