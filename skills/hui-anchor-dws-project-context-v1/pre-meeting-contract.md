# Pre-Meeting Output Contract

Use only the `context` from a validated `QwenProjectContextArtifact`. Do not call
DWS, read another file, add a source, or infer a new project fact.

Return one JSON object with exactly these fields:

```json
{
  "schema_version": 1,
  "artifact_type": "pre_meeting",
  "project_id": "copied from context",
  "project_name": "copied from context",
  "generated_at": "timezone-aware workflow time",
  "permission_scope": "copied from context",
  "last_decisions": [],
  "unfinished_actions": [],
  "current_risks": [],
  "verification_items": []
}
```

Copy every active decision into `last_decisions`, every `sourced_actions` item into
`unfinished_actions`, and every `sourced_risks` item into `current_risks` without
changing text or evidence. `verification_items` may select existing sourced actions
or risks; it cannot introduce a new fact. Missing sections stay empty.

Before reporting success, run `PreMeetingArtifact.model_validate` and then
`validate_pre_meeting_artifact` from `tools.qwenwork_project_artifacts` against the
same validated context. Any mismatch is a failed artifact, not permission to repair
evidence or invent a replacement.
