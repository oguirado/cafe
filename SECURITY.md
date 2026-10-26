# Security Policy

## Project status

Café is in development validation. It is an unfinished product, not a
finished application, and this policy makes no claim of a supported
release, a supported release window, an audited application, or a
vulnerability response time.

## Reporting a vulnerability

The repository has not been created remotely, so no GitHub reporting URL
exists yet. The reporting endpoint is not configured.

- Report through the existing private channel agreed with the operator.
- Never open a public issue containing secrets, credentials, tokens, or
  private data. A public report must never carry anything the public
  must not see.
- Describe the issue without copying private data into the report: what
  is affected, how it can be reached, and what an attacker would gain.
  Include the affected component and file path, steps to reproduce, and
  what you expected instead.
- There is no promised response time and no guaranteed fix window. Each
  report is reviewed as project work allows.

The repository-local commit identity shown in AGENTS.md is for
attribution only; its messaging address is not a monitored security
mailbox, so do not send reports there.

## Security principles for contributors

- Dependencies and versions need explicit approval. No unauthorized
  installs, and no new dependency without operator approval.
- Never pass raw handles, tokens, or credentials to the renderer or to
  MCP. The Rust core stays the authority; the renderer never holds
  authority.
- Never put credentials, tokens, or private data in code, logs, issues,
  fixtures, or reports. Report the presence of private data without
  copying it.
- Secret scanning runs as part of CI. A clean scan never excuses a
  secret committed to the tree; keep secrets out of every file you
  touch.

## Scope notes for reporters

Café captures the host's popped-out Discord stream and camera windows,
records each feed, sends capsules to OBS, and stores Twitch chat and
statistics as JSONL served through MCP. When reporting, state which
layer is affected: the Rust authority core in `core/`, the Python
connectors and MCP broker in `connectors/`, or the JavaScript with
JSDoc shell modules in `src/`. There is no authored TypeScript and no
SQL in the product.
