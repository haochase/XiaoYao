# Ascend Deployment Readiness

This runbook is the public D1 checklist for validating an Ascend-hosted
MiniCPM-o deployment. It contains only localhost Mock commands and sanitized
readiness checks. Keep provider endpoints, credentials, host details, and
device details in private operator notes.

## Scope and gate order

Run the checks in this order:

1. Validate the host workspace and NPU runtime with
   `ascend_runtime_probe.py`.
2. Validate the HTTP contract against the local MiniCPM-o Mock.
3. Validate the optional Realtime contract against the same local Mock.
4. On HiDevLab, repeat the sanitized probe and the endpoint smoke check with
   private operator values.
5. Stop the NPU-backed workload as soon as the required evidence is captured.

A local Mock proves the gateway contract only. It is not evidence that a
provider instance, a platform-specific backend, or a physical device is ready.

## 1. Runtime probe

Run from the repository root. The workspace path is a disposable, private
workspace; do not redirect output into a tracked file.

```powershell
$env:PYTHONPATH='gateway\src'
C:\Users\chase\miniconda3\python.exe tools\ascend_runtime_probe.py `
  --workspace .vendor\ascend-probe `
  --require-npu --json
```

The probe returns sanitized JSON. It records command availability, exit code,
and a SHA-256 digest of command output rather than retaining raw NPU output.

The `--require-npu` contract is explicit:

- Exit code `0` means `status=ready`: the workspace is writable and
  `npu-smi info` is available with exit code `0`.
- Exit code `1` means `status=blocked`: the workspace is not writable, the
  NPU command is missing, or the NPU command returned a non-zero exit code.
- Package versions and environment-presence flags are informational; they do
  not override a blocked required-NPU result.

Do not keep an NPU instance or process running while idle. After the probe and
the checks needed for the current gate, stop the workload, release the
allocated NPU resources, and shut down the instance according to the
HiDevLab project procedure. A blocked probe is a reason to fix the environment,
not a reason to leave a paid resource running.

## 2. Local MiniCPM-o Mock

Start the Mock in one terminal and leave it in the foreground:

```powershell
$env:PYTHONPATH='gateway\src'
C:\Users\chase\miniconda3\python.exe -m uvicorn `
  companion_gateway.voice.mock_minicpm_o:app `
  --host 127.0.0.1 --port 9000
```

In a second terminal, run the HTTP check first:

```powershell
C:\Users\chase\miniconda3\python.exe tools\minicpm_o_endpoint_check.py `
  --mode http --endpoint http://127.0.0.1:9000/v1/infer `
  --fixture assets\audio\companion-greeting-zh-cn.wav --turns 3 --json
```

Then run the optional Realtime check:

```powershell
C:\Users\chase\miniconda3\python.exe tools\minicpm_o_endpoint_check.py `
  --mode realtime `
  --endpoint ws://127.0.0.1:9000/v1/realtime?mode=audio `
  --fixture assets\audio\companion-greeting-zh-cn.wav --turns 3 --json
```

Both checks should return `status=ok`, the selected mode, three turns, and
sanitized per-turn metrics. The checker does not print audio payloads,
response text, credentials, or raw request data. Stop the Mock with
`Ctrl+C` after the checks.

## 3. HTTP-first fallback

HTTP is the required first path for D1 endpoint validation. Realtime is an
additional capability check, not a prerequisite for the HTTP path.

If the Realtime service is unavailable or does not implement the documented
audio contract:

1. Keep the HTTP result as the valid fallback result.
2. Record the result as `mode=http` and mark Realtime as unavailable.
3. Do not describe an HTTP result as Realtime or full-duplex evidence.
4. Continue to D2 only when the separate D2 handoff gate below is satisfied.

## 4. HiDevLab operator sequence

Run the same tools from a private HiDevLab workspace. Use the runtime probe
first, with `--require-npu --json`, then run the HTTP endpoint check before
attempting Realtime. Keep the service endpoint and authentication lookup in
the private operator command sheet; do not copy those values into this
repository or into public logs.

The endpoint checker accepts an environment-variable name through
`--auth-env`. The value is read at runtime and is never printed. Omit
authentication for the local Mock commands above.

## D2 handoff gate

D2 may add a platform-specific MiniCPM backend plan only after a real
HiDevLab probe JSON is available privately and shows:

- `status=ready`;
- a writable workspace;
- an available NPU command with exit code `0`; and
- the sanitized package and environment facts needed by the implementation
  owner.

The local Mock results and a local probe without `--require-npu` do not
satisfy this gate. If the real probe JSON is missing or blocked, stop at D1,
keep the NPU resource stopped, and do not draft a platform-specific backend
plan from assumptions.

## Private evidence checklist

Keep these artifacts outside the public repository and any public issue or PR:

- the real HiDevLab probe JSON and its command metadata;
- endpoint smoke output and service logs;
- provider project, instance, account, or host identifiers;
- endpoint URLs, credentials, and authentication material;
- local device identifiers, LAN details, and device screenshots.

Public reports should contain only pass/fail status, sanitized metrics, command
names, and the reason for any blocked gate.
