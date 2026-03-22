"""Output mode handlers for sandbox task completion."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import httpx

from lucent.sandbox.models import SandboxConfig

OutputMode = Literal["diff", "pr", "review", "commit"]


@dataclass
class OutputResult:
    """Structured result from output handling."""

    mode: OutputMode
    diff: str
    detail: str
    metadata: dict


class SandboxOutputHandler:
    """Applies output_mode behavior to completed sandbox tasks."""

    def __init__(self, manager, request_api, memory_api, logger):
        self._manager = manager
        self._request_api = request_api
        self._memory_api = memory_api
        self._log = logger

    async def process(
        self,
        *,
        sandbox_id: str,
        task_id: str,
        task_description: str,
        config: SandboxConfig,
    ) -> OutputResult | None:
        mode = config.output_mode
        if not mode:
            return None

        diff = await self._extract_diff(sandbox_id, config.working_dir)
        diff_detail = diff if diff.strip() else "No changes detected."
        await self._request_api.add_event(task_id, "sandbox_output_diff", diff_detail[:50000])
        if mode == "diff":
            return await self._handle_diff(task_id, diff)
        if mode == "review":
            return await self._handle_review(task_id, task_description, diff)
        if mode == "pr":
            return await self._handle_pr(sandbox_id, task_id, task_description, diff, config)
        if mode == "commit":
            return await self._handle_commit(sandbox_id, task_id, diff, config)
        raise ValueError(f"Unsupported sandbox output_mode: {mode}")

    async def _extract_diff(self, sandbox_id: str, working_dir: str) -> str:
        cmd = "git -c core.safecrlf=false diff"
        result = await self._manager.exec(sandbox_id, cmd, cwd=working_dir, timeout=120)
        if result.exit_code != 0:
            raise RuntimeError(f"git diff failed: {result.stderr.strip() or 'unknown error'}")
        return result.stdout or ""

    async def _handle_diff(self, task_id: str, diff: str) -> OutputResult:
        detail = diff if diff.strip() else "No changes detected."
        return OutputResult(mode="diff", diff=diff, detail=detail, metadata={"has_changes": bool(diff.strip())})

    async def _handle_review(self, task_id: str, task_description: str, diff: str) -> OutputResult:
        detail = diff if diff.strip() else "No changes detected."

        review_content = (
            f"Sandbox task review requested.\n\nTask ID: {task_id}\n\n"
            f"Task description:\n{task_description}\n\nDiff:\n{detail}"
        )
        created = await self._memory_api.create(
            type="experience",
            content=review_content,
            tags=["daemon", "needs-review", "sandbox-output"],
            importance=6,
            metadata={"task_id": task_id, "output_mode": "review"},
        )
        if created and created.get("id"):
            await self._request_api.link_memory(task_id, str(created["id"]), relation="created")

        return OutputResult(
            mode="review",
            diff=diff,
            detail="Created needs-review memory from sandbox diff.",
            metadata={"memory_id": created.get("id") if created else None},
        )

    async def _handle_pr(
        self,
        sandbox_id: str,
        task_id: str,
        task_description: str,
        diff: str,
        config: SandboxConfig,
    ) -> OutputResult:
        if not config.git_credentials:
            raise RuntimeError("output_mode=pr requires git_credentials")
        if not config.repo_url:
            raise RuntimeError("output_mode=pr requires repo_url")

        base_branch = config.branch or "main"
        branch_res = await self._manager.exec(
            sandbox_id,
            "git rev-parse --abbrev-ref HEAD",
            cwd=config.working_dir,
            timeout=30,
        )
        if branch_res.exit_code != 0:
            raise RuntimeError(
                f"failed to resolve current branch: {branch_res.stderr.strip() or branch_res.stdout.strip()}"
            )
        head_branch = (branch_res.stdout or "").strip() or base_branch
        remote_url = config.repo_url
        if remote_url.startswith("https://"):
            remote_url = remote_url.replace("https://", f"https://{config.git_credentials}@")

        set_remote = await self._manager.exec(
            sandbox_id,
            f"git remote set-url origin {shlex.quote(remote_url)}",
            cwd=config.working_dir,
            timeout=30,
        )
        if set_remote.exit_code != 0:
            raise RuntimeError(f"failed to set remote: {set_remote.stderr.strip() or set_remote.stdout.strip()}")
        if head_branch == base_branch:
            head_branch = f"lucent/task-{task_id[:8]}"
            push_ref = f"HEAD:refs/heads/{head_branch}"
        else:
            push_ref = head_branch
        push = await self._manager.exec(
            sandbox_id,
            f"git push -u origin {shlex.quote(push_ref)}",
            cwd=config.working_dir,
            timeout=120,
        )
        if push.exit_code != 0:
            raise RuntimeError(f"git push failed: {push.stderr.strip() or push.stdout.strip()}")

        owner_repo = self._parse_github_repo(config.repo_url)
        pr_url = None
        detail = "PR branch pushed."
        if owner_repo:
            pr_url = await self._create_github_pr(
                token=config.git_credentials,
                owner_repo=owner_repo,
                title=f"Sandbox task {task_id[:8]}",
                body=task_description,
                base=base_branch,
                head=head_branch,
            )
            if pr_url:
                detail = f"Created PR: {pr_url}"
            else:
                detail = "PR branch pushed, but PR creation failed; create PR manually."
        else:
            detail = "PR branch pushed, but repo_url is not a GitHub URL; create PR manually."
        await self._request_api.add_event(
            task_id,
            "sandbox_output_pr",
            detail,
            {"branch": head_branch, "base": base_branch, "repo_url": config.repo_url, "pr_url": pr_url},
        )
        return OutputResult(
            mode="pr",
            diff=diff,
            detail=detail,
            metadata={"branch": head_branch, "base": base_branch, "repo_url": config.repo_url, "pr_url": pr_url},
        )

    async def _handle_commit(
        self,
        sandbox_id: str,
        task_id: str,
        diff: str,
        config: SandboxConfig,
    ) -> OutputResult:
        if not config.commit_approved:
            raise RuntimeError("output_mode=commit requires commit_approved=true")
        if not config.git_credentials:
            raise RuntimeError("output_mode=commit requires git_credentials")

        branch = config.branch or "main"
        remote_url = config.repo_url or ""
        if remote_url.startswith("https://"):
            remote_url = remote_url.replace("https://", f"https://{config.git_credentials}@")
            await self._manager.exec(
                sandbox_id,
                f"git remote set-url origin {shlex.quote(remote_url)}",
                cwd=config.working_dir,
                timeout=30,
            )
        push = await self._manager.exec(
            sandbox_id,
            f"git push origin {shlex.quote(branch)}",
            cwd=config.working_dir,
            timeout=120,
        )
        if push.exit_code != 0:
            raise RuntimeError(f"git push failed: {push.stderr.strip() or push.stdout.strip()}")

        detail = f"Pushed commits to {branch}."
        await self._request_api.add_event(task_id, "sandbox_output_commit", detail, {"branch": branch})
        return OutputResult(mode="commit", diff=diff, detail=detail, metadata={"branch": branch})

    @staticmethod
    def _parse_github_repo(repo_url: str | None) -> str | None:
        if not repo_url:
            return None
        if repo_url.startswith("git@github.com:"):
            repo = repo_url.removeprefix("git@github.com:")
        else:
            parsed = urlparse(repo_url)
            if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
                return None
            repo = parsed.path.lstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        parts = repo.split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        return f"{parts[0]}/{parts[1]}"

    async def _create_github_pr(
        self,
        *,
        token: str,
        owner_repo: str,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> str | None:
        payload = {"title": title, "body": body[:65000], "base": base, "head": head}
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"https://api.github.com/repos/{owner_repo}/pulls",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    return data.get("html_url")
                self._log(f"GitHub PR creation failed ({resp.status_code}): {resp.text[:300]}", "WARN")
        except Exception as e:
            self._log(f"GitHub PR creation error: {e}", "WARN")
        return None
