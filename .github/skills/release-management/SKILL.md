---
name: release-management
description: 'Manage changelog updates, version bumping, Docker image tagging, and release notes generation'
---

# Release Management

Manage changelog updates, version bumping, Docker image tagging, and release notes.

## When to Use

- Preparing a new release
- Updating the changelog after completing features
- Bumping version numbers
- Creating release tags and notes

## Release Process

### Step 1: Update CHANGELOG.md

1. Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format
2. Move items from `[Unreleased]` to a new version section
3. Categorize changes: Added, Changed, Deprecated, Removed, Fixed, Security
4. Include meaningful descriptions (not just commit messages)

### Step 2: Bump Version

1. Update `version` in `pyproject.toml`
2. Follow [Semantic Versioning](https://semver.org/):
   - **MAJOR**: Breaking API changes
   - **MINOR**: New features, backward-compatible
   - **PATCH**: Bug fixes, backward-compatible
3. Search for any other version references that need updating

### Step 3: Docker Image

1. Build: `docker compose build`
2. Tag with version: `docker tag lucent:latest lucent:vX.Y.Z`
3. Verify the image runs correctly: `docker compose up -d`

### Step 4: Create Release

1. Commit version bump and changelog: `git commit -m "release: vX.Y.Z"`
2. Create a git tag: `git tag vX.Y.Z`
3. Push with tags: `git push origin main --tags`
4. Create GitHub release with `gh release create vX.Y.Z --notes-file <notes>`

### Step 5: Post-Release

1. Add new `[Unreleased]` section to CHANGELOG.md
2. Verify the release is visible on GitHub
3. Announce if appropriate

## Best Practices

- Never skip the changelog — it's the user-facing record of what changed
- Write changelog entries as you work, not all at release time
- Test the Docker image before tagging a release
- Keep release commits minimal (version bump + changelog only)
