# 会锚 DWS 项目资料同步

仅处理私有任务配置指定的一个项目，并严格按以下顺序执行：

1. 在项目工作目录运行 `tools/dws_project_sync.py collect`。参数仅使用私有任务配置中的
   `--manifest <PRIVATE_MANIFEST_PATH>`、`--project <PRIVATE_PROJECT_ID>`、
   `--dws-path <PRIVATE_DWS_PATH>` 和 `--output <PRIVATE_SOURCE_BUNDLE_PATH>`。
2. 读取私有来源包，调用项目记忆 Skill 生成
   `QwenProjectContextArtifact`，写入 `<PRIVATE_CONTEXT_ARTIFACT_PATH>`。每条事实性决策、
   行动项和风险都必须引用一个状态为 `active` 的来源，并使用该来源正文中的精确摘录。
   不得引用失败、删除或撤权来源，也不得根据常识、历史对话或模型推断补写事实。
3. 缺少证据时省略对应事实。只有已取得对应证据，才能将请求 ID 加入
   `completed_retrieval_request_ids`；未完成的检索请求绝不能加入
   `completed_retrieval_request_ids`，遗漏的请求保持 pending。不得捏造检索结果或
   完成状态。
4. 运行 `tools/dws_project_sync.py push`，参数仅使用私有任务配置中的 manifest、项目、
   来源包、context artifact、`--state-file <PRIVATE_STATE_PATH>`，gateway 固定为
   `http://127.0.0.1:8731`。令牌只由 `COMPANION_DWS_SYNC_TOKEN` 提供。
5. 用户可见输出只保留 CLI 返回的状态、计数、耗时和哈希字段。不得输出 DWS 命令、
   profile、资源 ID、来源标题或 URL、正文、摘录、私有路径、token、sync ID 或
   generation ID。

任一步失败即停止，不得绕过校验、拆分超限同步包、改用其他 gateway、追加 `--yes`，
也不得读取 manifest 白名单以外的钉钉资料。
