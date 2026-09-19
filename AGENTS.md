# Platform role contract

NetBox Config Context is the desired configuration source. The native inventory
flattens contexts; `infrabox_roles` is a mapping of role names to `{enabled: true|false}`.
Missing assignment means skipped. `enabled: false` never uninstalls or reverses
previous effects. Users and OpenClaw customize NetBox data, not executable code.
Only trusted maintainers change this repository.

- Keep `site.yml` generic. Never add per-host branches, synthetic coverage hosts,
  inferred dependencies, or an automatic dependency resolver.
- Role names come from `roles/*`; `role-order.json` lists each exposed role exactly
  once in deterministic execution order. Context key order is irrelevant. Unknown
  names fail even when disabled. Do not create a separate complex registry.
- Prefix every role input, default, registered value and runtime variable with the
  role name. Native Ansible variables/facts are exempt. Shared explicit contracts
  use `infrabox_*`. Never read another role's private variables.
- Document/default every public input in `roles/<role>/defaults/main.yml`. Never
  overwrite an input with `set_fact` or registered output. Use `<role>_runtime*`
  for runtime data; prefer task-local calculations over persistent host facts.
- Update `schemas/config-context.schema.json` whenever public inputs change.
  Individual contexts may be partial; schema validates names/structure/types,
  not completeness. Preserve native connection variables and unrelated metadata.
- Put mandatory-input and semantic assertions in `tasks/validate.yml`. The first
  play validates all selected hosts/roles before any role execution. Central
  validation owns registry, schema, identities and scope; do not build a generic
  semantic framework. Internal `infrabox_runtime_*` is not a context input.
- Every role must be idempotent and support meaningful `--check --diff`. Prefer
  state-based modules. Never use `check_mode: false` for a mutating task. Explicitly
  report deferred checks when a first-install dependency does not exist. Read-only
  commands may run during check mode and must honestly report `changed: false`.
- Preserve lifecycle tags (`install`, `configure`, `service`, `verify`) and role tags.
  An unchanged second apply must report `changed=0` absent external changes.
- Configure is the only supported pipeline entry for `site.yml`. PRs force check
  and diff; manual defaults to `limit=all, check=true`, and apply needs explicit
  `check=false` on the approved deployed revision. Never bypass the workflow with
  direct/ad-hoc Ansible against managed hosts or arbitrary extra flags.
- Each changed role needs at least one real managed NetBox host with that role
  enabled. Even one changed role without a target fails before Ansible execution,
  including when other changes force all-host scope. Do not invent a target.
- Role-only PRs select the union of assigned hosts. Shared/orchestration/schema/
  dependency/unknown paths conservatively select all. Do not parse task graphs.
- Same-repository PR authors are trusted; check mode is not a security sandbox.
  Never execute untrusted fork code on the credential-bearing runner. No root
  token, host runtime socket, secrets in Git/context/artifacts, or TLS bypass.
- Config Context changes require a concrete confirmed proposal. A live test must
  use explicitly authorized hosts; a previous discovery authorization is not an
  apply authorization. Preserve native Device/VM identities.

Role-change checklist:
1. Update role and its prefixed defaults.
2. Update schema for public-input changes and registry/order for new roles.
3. Ensure each changed role is assigned to at least one authorized real NetBox host.
4. Verify idempotence, check-mode behavior and validation-before-execution.
5. Run intended local syntax/schema/unit checks; local fixtures are not PR coverage.
6. Use configure PR validation for live check, then explicit manual apply as authorized.

Core deploys a committed source/ref and its compatible runtime. Keep changes in
this checkout committed before Core synchronization. Runtime-contract changes
require candidate runtime provisioning; role-only PRs need no runtime rebuild.
Promotion is through Core's selected canonical source/ref, not an automatic merge
or apply. Keep the protected execution branch and independent discovery lifecycle.

These instructions are self-contained. The project ADR is governance history,
not a prerequisite for an agent to implement this contract.
