# ADR-0016: Container-grade code isolation via unprivileged user namespaces

Date: 2026-09-13
Status: Accepted
Supersedes the "isolation grade" non-goal recorded in reconciliation-4.

## Context

`code.run` has always been honest about its trust boundary: the
SubprocessBoundary isolates against ACCIDENTS (crashes, hangs, leaked
environment), not ADVERSARIES. Reconciliation-4 recorded stronger
isolation as a deployment concern (Docker/gVisor). The continuation
mandate re-evaluated that boundary against the Universal Primitive Test:
*would a sandbox still make sense if education, WhatsApp, the model,
the provider, and the application all disappeared?* Yes. Isolation is
infrastructure. The question became: can container-grade isolation be
implemented WITHOUT introducing deployment dependencies (Docker
daemons, root daemons, external services)?

## Decision

WAX implements `NamespaceBoundary` — a sandbox built from the same
kernel primitives containers use (namespaces), requiring no external
dependencies, no root, and no daemons. It is the default for `code.run`
when the host supports unprivileged user namespaces (`isolation_backend:
auto`), and the isolation service degrades LOUDLY (log + metric
`isolation_fallback_total`) to SubprocessBoundary when it cannot.

### Kernel-enforced properties (each covered by an adversarial test)

| Property | Mechanism | Test |
|---|---|---|
| No network | fresh netns — no interfaces, no routes | `test_network_is_unreachable` |
| Read-only filesystem | `mount -o remount,ro,bind /` inside the namespace | `test_filesystem_is_read_only_outside_workspace` |
| Writable only the workspace | bind-remount rw at the ORIGINAL path (absolute paths keep working) | `test_workspace_is_writable_and_read_write_bound` |
| Host /proc masked | tmpfs over /proc (same-uid /proc/*/environ is a real secret channel) | `test_host_proc_is_masked` |
| Host /sys masked | tmpfs over /sys | (assembled with /proc; masked in all runs) |
| Private /tmp | size-capped, noexec, nodev, nosuid tmpfs | `test_private_tmp_hides_host_tmp` |
| Host processes invisible/unsignalable | fresh pidns (plus masked /proc) | `test_host_proc_is_masked` |
| No privilege escalation | unprivileged userns: "root" maps to the runtime uid only inside | probe + ro / |
| Fork-bomb containment | RLIMIT_NPROC | `test_fork_bomb_is_bounded` |
| Memory-bomb containment | RLIMIT_AS | `test_memory_bomb_is_bounded` |
| Disk-bomb containment | RLIMIT_FSIZE + tmpfs size cap | `test_disk_bomb_is_bounded` |
| No orphaned descendants | own session + process-group SIGKILL on timeout | `test_timeout_kills_the_whole_group` |
| Clean environment | scrubbed env (no inheritance) + masked /proc | `test_environment_is_scrubbed` |

### Selection policy

`isolation_backend: auto | namespace | subprocess` (WaxSettings).
- `auto` — namespace when the probe passes; otherwise subprocess with a
  warning log AND a metric. Silent degradation is forbidden.
- `namespace` — require it; unavailable is a configuration error.
- `subprocess` — legacy, weakest; the deployment accepts documented limits.

The capability result carries `isolation` (the grade that ACTUALLY ran)
and the runtime meters `code_executions_total{isolation=...}` —
action-level evidence of the enforcement grade, not an assumption.

### Honest limits

- A user namespace is not a microVM. Kernel exploits that escape
  userns confinement would apply. Deployments with truly hostile
  multi-tenant code implement `IsolationBoundary` with Docker/Firecracker;
  the contract and tests are unchanged.
- rlimits are governance (abuse bounds), not security — the namespace
  is the security boundary.
- The probe (`NamespaceBoundary.available()`) runs a real `unshare` once
  per process and caches; a host that rejects user namespaces (seccomp,
  AppArmor) falls back rather than failing code execution entirely.

## Consequences

- Adversarial code (prompt-injected, model-hallucinated, or malicious)
  can no longer: reach the network, read the runtime's environment via
  /proc, write anywhere outside the designated workspace, fork/allocate/
  write unboundedly, or survive its own timeout as an orphan.
- `IsolationKind.NAMESPACE` joins the contract; `ExecutionResult` and
  the `code.run` output now carry the enforcement grade for audit.
- The SubprocessBoundary also gains the process-group kill (its
  previous single-kill orphaned grandchildren — a real defect found
  while building this boundary).
- ~20 adversarial tests join the suite; they skip LOUDLY on hosts
  without userns support, where the runtime itself would also fall back.
