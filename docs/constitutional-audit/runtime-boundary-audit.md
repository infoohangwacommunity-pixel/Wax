# Runtime Boundary Audit — Constitutional Audit

Scope: module boundaries in `src/wax/`, what enforces each, and the layering law.

## Layer map (verified by import scan)

```
interfaces/  bridge/  app.py (composition root)     ← interface boundary
intelligence/adapters/  +  intelligence/service (factory)  ← provider boundary
────────────────────────────────────────────────────
core/ config/ identity/ state/ memory/ objective/ continuity/
capabilities/ execution/ isolation/ security/ authority/ agency/
resources/ reliability/ runtime/ observability/ media/
────────────────────────────────────────────────────
NEVER imports interface or provider modules
```

## Boundary enforcement status (before → after audit)

| Boundary | Claimed by | Was enforced by | Now enforced by |
|---|---|---|---|
| Core ⊬ interfaces | INV-02 | NOTHING (phantom test cited) | `tests/architecture/test_no_interface_coupling.py` (AST scan) — `bcec2a5` |
| Core ⊬ providers | INV-03 | NOTHING (phantom test cited) | `tests/architecture/test_no_provider_coupling.py` (AST scan) — `bcec2a5` |
| AI ⊬ direct effects | INV-04 | CapabilityInvoker (real, sole path) | unchanged + declared-contract validation now real |
| Core ⊬ domain | core invariants + comments | grep discipline | unchanged (verified clean this audit) |
| Principal scoping | state models | per-read-path checks (verified) | unchanged |
| Vendor policy ⊬ runtime | — (implicit) | nothing — Meta window lived in capabilities | `DeliveryPolicy` mechanism/policy split — `47887cb` |

## Findings

1. **The composition root is `runtime/app.py`** — the ONLY runtime module allowed to touch both the interface boundary and the runtime core. Clean.
2. **`wax.core` is import-clean** (`test_core_boundary.py` scans it): no upward imports.
3. **The one true layering defect found** was CV-1 (vendor policy in the capability layer) — fixed; the mechanism/policy boundary now has a real seam (`DeliveryRouter.policy_for`).
4. **Identity boundary table** (`INTERFACE_CREDENTIAL_KINDS`) was duplicated in three modules — the exact kind of drift that silently breaks interface handoff. Unified into `wax.identity.contracts`; the bridge now DERIVES its typed view from it.
5. **Runtime plumbing must not import the bridge** — second test in the interface-coupling file enforces the finer-grained rule inside `runtime/` itself.

## Verdict

The layering law is now what the invariants always claimed: machine-enforced at the two replaceable boundaries (interfaces, providers), mechanism-enforced at the agency boundary, and grep-verified domain-free. A future violation fails CI instead of surviving as folklore.
