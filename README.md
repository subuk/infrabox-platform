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

Core installs the operator's verified SSH host keys through
`platform_known_hosts_file`. An empty trust file does not auto-accept a target.
SSH and service TLS verification stay enabled. Python or other prerequisites
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
