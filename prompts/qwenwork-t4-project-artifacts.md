# 小千 T4 千问办公核心产物验收

只在当前对话完成本任务，不得新建、分叉或并行创建其他对话，不得委派给
新的 Agent。三个阶段必须依次执行并留在当前对话中；任一阶段失败即停止，
不得用旧产物、示例或普通工程 Agent 输出冒充成功。

## 1. 项目记忆

读取 `prompts/qwenwork-dws-project-manual-refresh.md`，只继承其门禁、生命周期步骤与失败处理，调用已启用的
`hui-anchor-dws-project-context-v1`，只使用白名单内本轮 active 来源生成并
验证项目记忆。必须完成真实生命周期；失败或需要人工审核时如实停止。
当且仅当第一阶段 `end=completed` 后继续第二、三阶段；人工刷新文件规定的
单个用户可见 CLI JSON 在本 T4 成功路径中不立即输出，用户可见的最终输出延迟到三个阶段全部结束。
任一失败路径仍立即输出固定失败状态并停止。

## 2. 会前要点

只使用第一阶段同一流程中已验证的项目记忆，读取 Skill 的
`pre-meeting-contract.md`。固定输出上次有效结论、未完成事项、当前风险和
本次待核验项；缺失内容留空，每条事实保留原来源。先执行
`PreMeetingArtifact.model_validate`，再执行 `validate_pre_meeting_artifact`，
两项都通过才可记录为完成。

## 3. 会后审核报告

读取本机 `http://127.0.0.1:8724/api/conflicts` 的同轮真实脱敏审核快照，只保留
accepted、rejected 和 proposed 记录；不得调用 8723、不得读取 Token。结合
第一阶段项目记忆，按 `post-meeting-contract.md` 固定区分已接受、已拒绝、
待确认和行动项，不得推测审批状态。先执行
`PostMeetingArtifact.model_validate`，再执行 `validate_post_meeting_artifact`，
两项都通过才可记录为完成。

## 4. 私有产物与证据

将三份人读产物、结构化校验结果和证据清单只写入固定目录
`E:\haochase\xiaoqian\小千项目文档\submission\t4`。执行前使用
`git rev-parse --show-toplevel` 确认当前 Git 根，并验证该目录是 E 盘普通目录、
必须位于 Git 工作树之外；任一条件不满足就报告 `private_output_invalid` 并停止。
不得创建仓库内 `submission/t4` 或 `.private` 替代目录。记录当前对话的真实 Session ID；不可获得就报告
`session_id_unavailable`，不得编造。标记三个适合截图的关键阶段位置。

最终只向聊天输出：三个阶段各自的状态、产物名称、校验状态、当前 Session ID
或固定不可用状态，以及截图位置。不得输出凭据、Token、profile、私有资源 ID、
来源正文、完整摘录、Base64、哈希或私有绝对路径。
