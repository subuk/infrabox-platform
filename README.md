# InfraBox Platform automation

Manually collect native Ansible facts from the managed NetBox inventory through
local Gitea. Core provisions the repository, runtime, runner, credentials and
deployment settings. This repository contains the fixed executable automation.

The Gitea dispatch/checkout/v4 artifact compatibility gate passed on the
development appliance. Managed-host acceptance is tracked in Core
IMPLEMENTATION_STATUS.md; compatibility alone does not establish target access.

## Before running

In NetBox, tag devices or VMs with `infrabox-managed`. Configure a connection
address through the ordinary NetBox inventory fields or Config Context. The
inventory uses `netbox.netbox.nb_inventory`, `config_context: true` and
`flatten_config_context: true`.

Put native Ansible variables directly in Config Context, for example:

```json
{
  "ansible_user": "discovery",
  "ansible_port": 2222
}
```

The port is optional: when absent, Ansible chooses its connection default. Set
`ansible_connection`, `ansible_network_os` and other variables normally for the
target platform. There is no application OS filter or custom variable mapping.
Contexts are trusted execution configuration: whoever can change them can affect
connections, interpreters and other Ansible behavior. They are not inert data.

For the initial default SSH credential, the operator installs its public key in
the target account and writes the private key to OpenBao KV v2:

| Setting | Default |
| --- | --- |
| KV mount | `kv` |
| Secret path inside the mount | `platform/ssh/default` |
| Field | `private_key` |
| Provisioned read-only NetBox credential | `platform/netbox`, field `token` |

Do not paste private keys into Config Context, Git, Actions variables or logs.
Use OpenBao's UI or protected file/stdin input. Core creates only the NetBox token;
it never generates or overwrites the operator's target SSH key. The key is copied
into a private per-run file; `ANSIBLE_PRIVATE_KEY_FILE` supplies the default, while
native host variables retain their normal precedence. The current workflow expects
this default key to exist before discovery starts.

SSH uses trust on first use (`StrictHostKeyChecking=accept-new`): the first
connection automatically records the presented host key in
`/run/platform/ssh-trust/known_hosts`; a changed known key is rejected. The first
key is not independently verified. Core mounts persistent writable host storage
for this file, preserving it across runner recreation, reboots and Ansible reruns.
Adding a target requires no Ansible run. Service TLS verification stays enabled.
See the Core Platform guide for handling a legitimate host-key replacement. Python or other prerequisites
required by the selected Ansible connection/facts module must already exist on
the host. The playbook does not install packages or intentionally alter target
configuration; normal Ansible temporary module execution is expected.

## Run and read results

Open local Gitea → **infrabox-platform/automation** → **Actions** →
**Discover host facts** → **Run workflow**.

`targets` is an Ansible pattern. Empty means the entire managed inventory.
Examples: `host1`, `host1,host2`, `site_lab`, `platform_linux:!site_remote`, `web*`.
Inventory groups use the native singular prefixes `platform_`, `site_`, `role_`,
`tag_`, and the native `is_virtual` grouping. Verify the names in your NetBox data.
Unmatched expressions follow Ansible semantics; zero selected hosts explicitly
fails with `no_targets`. Patterns are not shell commands or arbitrary CLI options.
The UI does not accept local pattern-file paths such as `@file`.

The **Discover native facts** step prints counts and a per-host summary. Download
the `discovery-<run-id>-<attempt>` artifact from that run. It contains:

- `summary.md`: selected/succeeded/failed/unreachable/not_completed counts and
  available OS/version, kernel, logical CPUs and memory MiB.
- `run.json`: run/attempt, UTC timestamps, executed SHA, requested pattern,
  collection outcome, return code and host/NetBox identity/file mapping.
- `facts/device-<id>.json` or `facts/vm-<id>.json`: native `ansible_facts` keys and
  values, without a normalized facts schema.

Missing fields are `unknown`. A failed host does not discard another host's
successful results. Partial collection fails the workflow. The upload step runs
after collection failure and has its own visible outcome. Upload failure also
fails the workflow; it does not mean collection necessarily failed. Cancellation
or appliance death can prevent finalization/upload. A canceled run is not success.

The repository/artifacts are private. Upload requests specify seven-day retention;
actual Gitea enforcement must be checked during live acceptance. Credentials,
raw contexts/inventory and unrestricted diagnostic dumps are not uploaded.
Failed hosts include bounded error messages in `run.json` and `summary.md`, also
printed in the discovery step log. Failed Ansible processes include captured
diagnostics. Known runtime credentials and common secret fields are redacted;
Ansible `no_log` failures remain suppressed.
Environment, local, facter and ohai fact families are excluded from publication.
For `setup`, module defaults also disable those collectors and executable local
facts. Other fact modules keep their supported arguments and native output.
Facts remain sensitive infrastructure information even without these fields.

There is no discovery scheduler, pipeline monitoring or NetBox writeback.

## Runtime and updates

The runtime recipe pins its Python, Node and runner base images by digest. Debian
packages come from a fixed snapshot. Python dependencies are pinned in
`requirements.txt`; the Ansible package supplies its bundled collection set and
`requirements.yml` pins the NetBox/Vault integrations explicitly.

This is a finite dependency set, not a promise that every Ansible collection or
transport is available. Missing dependencies are reported as failures. Add and
pin required dependencies through reviewed Platform updates.

The workflow uses external checkout v4.2.2 and the Gitea-compatible
`ChristopherHX/gitea-upload-artifact` v4 fork, pinned by commit SHA. Gitea's REST
artifact listing/download endpoints expose v4 artifacts. Fetching those Actions
can require access to GitHub. The
Platform source itself is checked out from local Gitea. Node is included for
Actions compatibility. No host container-engine socket or privileged job is used.

Core force-synchronizes the chosen source/ref to the local execution branch,
initially `master`. Users have Code Read / Actions Write; provisioning controls
code. Local edits are unsupported and replaced by the next update. Repository
settings, run history and artifacts are preserved. Core stages the candidate
runtime before pausing/draining the runner and applying the matching revision.
Failed fetching/building preserves the preceding deployment. A failed cutover
may leave the runner paused until the owning Core role is rerun successfully.

For development, Core can bundle a committed local checkout and transfer it to
the appliance. No GitHub push is required; uncommitted files are never included.
See Core's `docs/platform.md` for deployment and recovery commands.

## Local checks

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/ansible-galaxy collection install -r requirements.yml -p .ansible/collections
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -v
```

The focused fixtures exercise actual Ansible selection and local fact gathering,
plus result confidentiality and partial failure. They do not replace the real
Gitea/manual-dispatch/artifact and managed-host acceptance checks.


## KRG-21 reconciliation

After collection, `scripts/reconcile_netbox.py` consumes native per-host facts,
validates a compact typed model and applies explicit NetBox diffs through pinned
pynetbox. The dedicated svc-platform identity obtains its write-enabled token
from the existing OpenBao path. Optional Linux `dmidecode --type 4,17` uses become;
unavailable/failed DMI only warns. CPU/DIMM observations map to native module bays,
module types/profiles and modules; guests never generate physical hardware.

`reconciliation-<run>-<attempt>` is the compact schema-v2 report for OpenClaw.
`discovery-<run>-<attempt>` retains raw filtered facts and DMI for human debugging.
Both expire after seven days. Gateway never downloads the raw artifact. The
workspace is cleaned after publication; `/data/discovery-health.json` retains
compact monitoring state across runner restarts. Unchanged observations generate
no infrastructure writes; fresh successful collection updates provenance only.

Ownership, ambiguity rules and authorization boundaries are documented in Core's
`docs/discovery-reconciliation.md`. Run focused tests with
`python -m unittest discover -s tests -p test_reconciliation.py`.

Discovery excludes interface names matching `cilium_*` and `lxc*` from NetBox
reconciliation, including their MACs and IPs. Raw facts remain complete. Override
`PLATFORM_INTERFACE_EXCLUDE_PATTERNS` in Gitea repository variables with comma-separated,
case-sensitive shell globs; use `,` to disable exclusions (an empty repository value
uses the default). Existing NetBox interfaces are never automatically deleted.

Disk reconciliation uses existing Linux `ansible_devices` facts. Whole `sd`, `hd`,
`vd`, `xvd`, NVMe and MMC block devices are considered; partitions, loop/zram,
device-mapper/LVM, software RAID and optical devices are not separate inventory assets.
Repeated paths sharing a WWN or serial are collapsed; conflicting observations warn
and are skipped. Physical disks require a model and stable WWN/serial (NVMe requires
namespace EUI/UUID/WWN); unknown vendor uses the existing explicit generic manufacturer.
Module profile `Discovery Disk` records exact bytes and rotational status; module
custom field `discovery_disk_identity` retains identity. Its bay represents an
observed disk identity, not an inferred chassis slot. Missing disks are never deleted.
VM disks use native VirtualDisk name/size, with decimal MB rounded up, matching
InfraBox's current NetBox `DISK_BASE_UNIT=1000`. Changing that NetBox setting requires
updating the conversion. Passthrough host/guest ownership is not inferred from guest
facts; guests registered as Device are still reported without physical modules.

## Configure hosts from NetBox (KRG-10)

`site.yml` is the generic configuration entry point, available only through
**Configure managed hosts** in local Gitea Actions. Discovery remains independent.
The initial roles support AlmaLinux 10/systemd: `packages`, `chrony`, `sshd`.
Public settings are documented in each role's `defaults/main.yml` and in
`schemas/config-context.schema.json`. Example partial context:

```json
{
  "infrabox_roles": {
    "packages": {"enabled": true},
    "chrony": {"enabled": true},
    "sshd": {"enabled": true}
  },
  "packages_names": ["curl"],
  "chrony_pools": ["2.almalinux.pool.ntp.org"],
  "chrony_dhcp_sources": true,
  "sshd_log_level": "INFO"
}
```

Use native NetBox context assignment and merging. Core provides the **InfraBox
Platform** Config Context Profile, synchronized from the schema of the approved
Git source. Assign it to Platform contexts. NetBox 4.7 does not apply that profile
to Device/VM local context, so configure independently validates the effective
inventory including local overrides. Partial contexts need not contain all role
inputs. Unknown role names, mistyped namespaced settings and reserved runtime
variables fail validation. Disabled roles are skipped, not uninstalled.

Role order belongs to `role-order.json`, never context mapping order. Configure
loads the normal dynamic inventory once, privately snapshots it including groups,
and uses that same snapshot for scope/preflight/execution. All selected hosts pass
preflight before any configuration role starts. Temporary credentials and snapshot
are removed after execution. Raw contexts are never published.

Manual dispatch accepts only `limit`, `check`, `diff`. Defaults are `all`, `true`,
`true`; applying requires explicit `check=false` on the deployed revision. Native
Ansible patterns select hosts; zero targets fail. No arbitrary extra flags,
inventory overrides or alternate playbooks are supported. Do not run ad-hoc
Ansible directly against managed hosts.

PRs check out the exact reviewed head and force `--check --diff`. Role-only changes
select the union of hosts enabling changed roles. Global/shared/unknown paths
select all managed hosts. Every changed role must have a real assigned target;
one role without a target fails the whole job before Ansible execution. README,
AGENTS and `docs/` changes alone require no host execution. Unit fixtures never
count as PR target coverage. A role-only PR validates all enabled roles on its
selected hosts, not only changed tasks.

Only trusted same-repository code authors may use this credential-bearing runner.
Fork PRs are excluded; Ansible check mode is not a sandbox for arbitrary code.
PR policy and runtime compatibility checks run from `/opt/platform`, not a mutable
checkout entry point. Requirements/runtime/policy-helper changes need a compatible
candidate runtime provisioned through Core before the check can pass. Role-only
changes can use the existing compatible runtime. Discovery retains its exact SHA
runtime contract. Manual configure requires the currently deployed SHA.

Promotion remains Core's selected canonical source/ref: retain the reviewed commit
in the canonical source checkout, then deploy it through Core. Do not merge a
local Gitea branch independently and assume Core will retain it. Core synchronizes
only the protected execution branch, preserving PR branches and history.

The `configure-<run>-<attempt>` artifact records executed/base revision, runtime
fingerprint, mode, scope, selected hosts, task differences and host recap. Failures
and partial applies remain failures. An interrupted apply is not transactional;
repair desired state/code and rerun the same workflow. Nothing auto-rolls back.
`sshd` manages only log level and client-alive settings, requires the standard
leading drop-in include, validates the candidate/full daemon config and reloads.
Ports, authentication, keys and access migration are outside the initial contract.
First-install check mode reports deferred service checks without installing a
package outside check mode. Check mode cannot prove runtime health after apply.

Local focused validation (with the pinned dependencies and collections installed):

```sh
.venv/bin/python -m unittest discover -s tests -p test_configure.py -v
ANSIBLE_STDOUT_CALLBACK=default .venv/bin/ansible-playbook -i localhost, site.yml --syntax-check
```

These local fixtures do not contact managed hosts. See `AGENTS.md` for the complete
self-contained role-change checklist and authorization requirements.
