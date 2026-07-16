# YANG Modules

This directory vendors the Cisco IOS XE YANG models that the handlers in
`labs/network-automation/handlers/` depend on. Two versions are kept side by
side because the codebase branches behaviour between them at runtime.

## Why these are in the repo

The handlers reference specific YANG paths and namespaces. When Cisco changes
a model structure between IOS XE versions (e.g. OSPF's `<mask>` becoming
`<wildcard>` in 17.x, or the DHCP pool `default-router` leaf-list becoming
a container in 17.x), the handler code branches via runtime version
detection.

Without the YANG source files in version control, the *why* of every such
branch lives only in commit messages and external Cisco docs. Vendoring
them makes the reasoning auditable: `git diff yang/ios-xe-1681/Cisco-IOS-XE-ospf.yang yang/ios-xe-1731/Cisco-IOS-XE-ospf.yang`
shows exactly what changed and therefore why the handler branches.

## Why these versions

The two versions correspond to the two IOS XE major releases the codebase
actively supports:

| Directory       | IOS XE version | Devices in lab today                       |
|-----------------|----------------|--------------------------------------------|
| `ios-xe-1681/`  | 16.8.1         | `lab-dc-h-vm10` (CSR1000v, 16.9.5)         |
| `ios-xe-1731/`  | 17.3.1         | `LAB-R11-C01-R01` (ISR4221, 17.3.4a), `lab-dc-h-sw01` (C9200L, 17.6.3) |

If a future device runs a version with materially different YANG structures
(e.g. 17.6.x's revised DHCP container layout), add a new `ios-xe-XXXX/`
directory rather than overwriting these.

## Why only these five modules

Only the modules the handlers actually touch:

| Module                       | Used by handler(s)                                                |
|------------------------------|-------------------------------------------------------------------|
| `Cisco-IOS-XE-native`        | All interface handlers (parent module)                            |
| `Cisco-IOS-XE-interfaces`    | `interface_description`, `interface_ip`, `interface_state`, `interface_switchport`, `hsrp` (submodule of native) |
| `Cisco-IOS-XE-ospf`          | `ospf` (augmenting module, separate namespace)                    |
| `Cisco-IOS-XE-ip`            | `static_routes`, `dhcp_relay` (submodule of native)               |
| `Cisco-IOS-XE-dhcp`          | `dhcp_server`                                                     |

Modules not used by any handler are not vendored to keep the repo focused.
The full set lives upstream at `github.com/YangModels/yang`.

## Source and licensing

Files are unmodified copies from:

  https://github.com/YangModels/yang/tree/main/vendor/cisco/xe/

Each file retains its original Cisco copyright header. They are redistributed
under the same license terms (Cisco's BSD-style license for IOS XE YANG
models — see the file headers themselves).

## Refreshing

To re-pull the files from upstream (e.g. after a Cisco model revision):

```bash
./fetch_yang.sh
```

The script is idempotent — it overwrites existing files with the upstream
versions. Diff the result before committing to see what changed.

## Validation

To validate a YANG file syntactically:

```bash
pip install pyang
pyang --strict yang/ios-xe-1731/Cisco-IOS-XE-ospf.yang
```

For deeper inspection (rendering the tree, walking the augmentations), YANG
Suite is the standard tool. See `docs/network_automation_documentation.md`
§3.6 for the local installation instructions.
