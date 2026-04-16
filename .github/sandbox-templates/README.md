# Built-in Sandbox Templates

Each `*.yaml` file in this directory defines a built-in sandbox template that
the cognitive planner can reference by name. Built-in templates have
`scope = "built-in"` and `status = "approved"` — they can be used by any task
in the organization without further review.

The planner is required to pick a built-in (or organization-approved) template
when it creates a sandboxed task. If no existing template fits the work, the
planner uses the `propose_sandbox_template` MCP tool to submit a new design
that goes into the review queue (status = `proposed`) for a human to approve.

## Schema

```yaml
name: string                        # unique within the organization
description: string                 # short human-readable summary
image: string                       # docker image (must be allowlisted)
working_dir: string                 # default /workspace
network_mode: none|bridge|allowlist # default none
allowed_hosts: [string, ...]        # required when network_mode = allowlist
setup_commands: [string, ...]       # commands to run after clone, pre-task
env_vars: { KEY: VALUE }            # static env vars (no secrets!)
memory_limit: string                # e.g. "2g"
cpu_limit: float                    # e.g. 2.0
disk_limit: string                  # e.g. "10g"
timeout_seconds: int                # max wall-clock time
```

## When to add a new built-in

A new built-in template is justified when the same configuration shows up in
multiple proposed templates from the planner. One-off configs should stay as
organization-scoped approved templates rather than ship as built-ins.
