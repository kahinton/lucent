---
name: release-management
description: 'Manage changelog updates, version bumping, tagging, and release notes. Use when preparing a release, updating changelog, bumping versions, or creating release notes.'
---

# Release Management

## Release Procedure

### 1. Update the Changelog

Follow [Keep a Changelog](https://keepachangelog.com/) format:

1. Move items from `[Unreleased]` to a new version section: `[X.Y.Z] - YYYY-MM-DD`
2. Categorize changes: **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**
3. Write entries as user-facing descriptions, not commit messages
4. Include breaking changes prominently at the top of the version section

### 2. Bump the Version

Follow [Semantic Versioning](https://semver.org/):

| Change type | Version bump | Example |
|------------|-------------|---------|
| Breaking API changes | **MAJOR** | 1.0.0 → 2.0.0 |
| New features, backward-compatible | **MINOR** | 1.0.0 → 1.1.0 |
| Bug fixes, backward-compatible | **PATCH** | 1.0.0 → 1.0.1 |

Update the version in the project's manifest file (`pyproject.toml`, `package.json`, `Cargo.toml`, etc.). Search the codebase for any other version references that need updating.

### 3. Build and Verify

```bash
# Build
docker compose build                   # or the project's build command

# Tag
docker tag <image>:latest <image>:vX.Y.Z

# Verify — the built artifact should start and pass health checks
docker compose up -d
curl -s http://localhost:<port>/health
```

### 4. Tag and Publish

```bash
git add -A
git commit -m "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

Create a GitHub release:
```bash
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file RELEASE_NOTES.md
```

### 5. Post-Release

1. Add a new `[Unreleased]` section to the changelog
2. Verify the release is visible and the artifacts are correct
3. Record the release:

```
create_memory(
  type="technical",
  content="## Release vX.Y.Z\n\n**Date**: <date>\n**Highlights**: <key changes>\n**Breaking**: <any breaking changes>\n**Notes**: <anything worth remembering for next release>",
  tags=["release"],
  importance=6,
  shared=true
)
```

## Rules

- Never skip the changelog — it's the user-facing record of what changed
- Write changelog entries as you work, not all at release time
- Test the built artifact before tagging — don't tag a broken release
- Keep release commits minimal: version bump + changelog only