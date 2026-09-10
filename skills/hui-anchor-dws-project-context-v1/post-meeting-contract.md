# Post-Meeting Output Contract

Input is a validated project context plus the same-run filtered 8724 review snapshot.
The snapshot contains only `accepted`, `rejected`, and `proposed` candidates. Treat
it as audit data: 不得推测审批状态，不得把模型判断写成人工批准。

Return one JSON object with exactly these fields:

```json
{
  "schema_version": 1,
  "artifact_type": "post_meeting",
  "project_id": "copied from context",
  "project_name": "copied from context",
  "generated_at": "timezone-aware workflow time",
  "permission_scope": "copied from context",
  "accepted": [],
  "rejected": [],
  "pending": [],
  "action_items": []
}
```

Copy each review record without changing IDs, old/new decision text, reason, status,
reviewer, time, review reason, or optional evidence. Put `accepted` records only in
`accepted`, `rejected` records only in `rejected`, and `proposed` records only in
`pending`. Copy project `sourced_actions` into `action_items`; missing sections stay
empty. Never disclose the artifact in public logs.

Before reporting success, run `PostMeetingArtifact.model_validate` and then
`validate_post_meeting_artifact` from `tools.qwenwork_project_artifacts` against the
same context and exact review snapshot. Any missing, fabricated, or reclassified
record fails validation.
