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
| 0.1.x   | ✅        |

## Scope

The following are in scope for security reports:

- Authentication and authorization bypasses
- SQL injection or other injection attacks
- Unauthorized access to memories or user data
- API key leakage or mishandling
- Denial of service vulnerabilities
- MCP protocol security issues

## Disclosure Policy

We follow coordinated disclosure. Once a fix is available, we will:

1. Release a patched version
2. Publish a security advisory on GitHub
3. Credit the reporter (unless they prefer anonymity)
