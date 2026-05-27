# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Lucent, please report it responsibly.

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, please email **security@lucent.dev** (or open a [private security advisory](https://github.com/kahinton/lucent/security/advisories/new) on GitHub) with:

- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You should receive a response within 72 hours acknowledging your report. We will work with you to understand and address the issue before any public disclosure.

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.2.x   | ✅        |
| < 0.2   | ❌        |

## Scope

The following are in scope for security reports:

- Authentication and authorization bypasses
- SQL injection or other injection attacks
- Unauthorized access to memories or user data
- API key leakage or mishandling
- Denial of service vulnerabilities
- MCP protocol security issues

## Session Management

Lucent uses a **single-session-per-user** model by design. Each user can have only one active session at a time — logging in on a new device or browser immediately invalidates the previous session.

This is an intentional security choice, not a limitation:

- **Limits blast radius of credential theft.** If a session token is compromised, the attacker's access is revoked as soon as the legitimate user logs in again.
- **Matches Lucent's deployment model.** Lucent is an admin/developer tool typically used by one person at one workstation, not a consumer app requiring simultaneous multi-device access.
- **Simpler security posture.** No need for session listing, revocation UI, or background cleanup of expired sessions.

If multi-device support becomes necessary in the future, a clean migration path exists: a dedicated `sessions` table (many-to-one with users) can replace the current single-token column without breaking the authentication flow.

## Disclosure Policy

We follow coordinated disclosure. Once a fix is available, we will:

1. Release a patched version
2. Publish a security advisory on GitHub
3. Credit the reporter (unless they prefer anonymity)
