# Output Contract

The surrounding workflow supplies a schema-version-1 `DwsSourceBundle` containing
project_id, project_name, permission_scope, collected_at, records, optional
retrieval_requests, and a verified content_hash. Do not recompute source metadata
or invent fields missing from the supplied records.

Start with this shape, replacing identity/time with exact input values:

```json
{
  "schema_version": 1,
  "context": {
    "project_id": "example-project",
    "project_name": "Example",
    "generated_at": "2026-09-05T12:00:00Z",
    "permission_scope": "project:example",
    "freshness_seconds": 1800,
    "source_refs": [],
    "active_decisions": [],
    "open_actions": [],
    "current_risks": [],
    "next_meeting": null,
    "sourced_actions": [],
    "sourced_risks": [],
    "sourced_next_meeting": null
  },
  "completed_retrieval_request_ids": []
}
```

Example values above are format examples, never a fallback project or source.

An EvidenceRef has exactly source_type, source_id, source_title, source_url,
source_time, excerpt, permission_scope. Copy every metadata field from one active
record. `excerpt` 只能来自单个非标题正文片段的连续原文，不拼接、不省略、不改标点，
最长 150 字；若不能满足就省略该事实。
source_type is document, meeting_note, task or calendar in this workflow.

A SourcedFact is `{"text": "supported statement", "source_refs": [EvidenceRef]}`.
text is nonblank and at most 2000 characters. At least one supporting ref is required.
Use this shape in sourced_actions, sourced_risks and optional sourced_next_meeting.

A DecisionCard has decision_id, project_id, topic, decision_text, rationale, owner,
decided_at, source_refs, status and confidence. Only use it when the source explicitly
supplies an approved decision, owner, rationale and timezone-aware decided_at.
Set status to active, confidence between 0 and 1 based on source clarity, not approval
probability. Every required text field must be nonblank. Limits: decision_id 128,
topic 512, decision_text/rationale 2000, owner 256 characters. Generate decision_id as
`decision_` plus the first 24 lowercase hex characters of SHA-256 over the UTF-8 string
`source_type + "\n" + source_id + "\n" + decision_text` (literal newline separators).
This stable identifier is a derived local key, not a fabricated upstream resource ID.

## Atomic DecisionCard Rules

每个 DecisionCard 只能表达一个可独立提问、独立审批、独立变化的主题
硬件选型和会前提醒时间必须生成不同的 DecisionCard
来源未明确支持的主题必须省略，不得依据模型常识拆分或补写
已有组合人工批准不得自动迁移为两张分别批准的 DecisionCard

`Generate exactly one artifact` 指一个 JSON artifact，不限制 `active_decisions`
卡片数量。同一 active 来源段落可以支持多张卡，但每张卡必须有自身连续 excerpt，
禁止拼接。拆分后每张卡按自身 `decision_text` 重新计算稳定 `decision_id`；不得为
沿用旧 ID 保留组合文本。Skill 只能使用当前 DwsSourceBundle，不能读取或复制历史
`approval_ref`；无来源依据时省略。当前组合 v2 不属于本任务迁移对象。

In the repository, `tools.dws_project_sync.QwenProjectContextArtifact` and
`tools.dws_sync.adapters.DwsSourceBundle` are the executable schema authority.
The caller validates your output and exact reference binding before any write.
Do not import code or access the repository from inside this skill; validation is
the caller's responsibility. A mismatch is a failure, not permission to guess fields.
