"""Built-in tools for the LangChain engine.

The Copilot SDK engine ships provider-native built-in tools (bash, view,
str_replace_editor, create_file, edit_file, grep) that are auto-injected into
every session. The LangChain engine historically only received MCP tools, so
local/Ollama models could not read files, edit code, run commands, or fetch the
web — any tool-dependent task failed validation with "no operational tool calls".

This module gives the LangChain engine a parity toolset implemented as plain
OpenAI-style function schemas plus an executor, so it slots into the existing
``bind_tools`` / tool-call loop alongside the MCP bridges.

Safety model (these run in-process, not in a container, mirroring Copilot's
built-ins which also run in the daemon's working directory):

- **Path confinement**: file tools resolve every path against a root directory
  and reject anything that escapes it (``..`` traversal, absolute paths outside
  root, or symlinks pointing outside root).
- **SSRF protection**: ``web_fetch`` and ``web_search`` validate every URL —
  including each redirect hop — with :func:`lucent.url_validation.validate_url`,
  blocking private/loopback/link-local ranges and cloud metadata endpoints.
- **Command blocklist**: ``run_shell`` blocks a small set of clearly
  destructive / exfiltration patterns and reads of secret files, and always
  runs with a timeout and bounded output.
- **Bounded output**: every tool truncates output to keep the model context
  from blowing up.

Toggles (environment variables):

- ``LUCENT_LANGCHAIN_BUILTIN_TOOLS`` (default ``1``): master switch.
- ``LUCENT_LANGCHAIN_ALLOW_SHELL`` (default ``1``): enable ``run_shell``.
- ``LUCENT_LANGCHAIN_ALLOW_NETWORK`` (default ``1``): enable ``web_fetch`` and
  ``web_search``.
- ``LUCENT_LANGCHAIN_TOOLS_ROOT`` (default: process CWD): file/shell root.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from lucent.logging import get_logger
from lucent.url_validation import SSRFError, validate_url

logger = get_logger("llm.builtin_tools")

# Maximum characters returned from any single tool call.
_DEFAULT_MAX_OUTPUT = 30_000
# Maximum bytes read from disk or the network for a single call.
_MAX_READ_BYTES = 2_000_000
# Default and ceiling timeouts (seconds) for shell / network operations.
_DEFAULT_SHELL_TIMEOUT = 120
_MAX_SHELL_TIMEOUT = 600
_DEFAULT_FETCH_TIMEOUT = 30
_MAX_FETCH_TIMEOUT = 120
_MAX_REDIRECTS = 5

# Patterns that are blocked outright in run_shell. Defense-in-depth, not a
# substitute for a sandbox — these catch the obviously catastrophic cases.
_SHELL_BLOCKLIST: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*r[a-zA-Z]*\s+)?/(?:\s|$)"), "recursive root delete"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"), "rm -rf"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}"), "fork bomb"),
    (re.compile(r"\bmkfs\b|\bdd\s+if=.*of=/dev/"), "disk overwrite"),
    (re.compile(r">\s*/dev/sd[a-z]"), "raw disk write"),
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b"), "pipe-to-shell"),
    (re.compile(r"\.env\b|\.daemon_api_key\b|id_rsa\b|/etc/shadow\b"), "secret file access"),
    (re.compile(r"\bgit\s+push\b.*(--force|-f)\b"), "force push"),
]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


class BuiltinToolset:
    """A confined set of in-process tools for the LangChain engine."""

    def __init__(
        self,
        *,
        root_dir: str | os.PathLike[str] | None = None,
        allow_shell: bool = True,
        allow_network: bool = True,
        max_output_chars: int = _DEFAULT_MAX_OUTPUT,
    ) -> None:
        self.root = Path(root_dir or os.getcwd()).resolve()
        self.allow_shell = allow_shell
        self.allow_network = allow_network
        self.max_output = max_output_chars

    # -- path safety --------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Resolve *path* against the root, rejecting any escape from root."""
        if not path or not isinstance(path, str):
            raise ValueError("path is required")
        candidate = Path(path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()
        # Confinement check via realpath/commonpath so symlinks can't escape.
        root_str = str(self.root)
        if os.path.commonpath([root_str, str(resolved)]) != root_str:
            raise ValueError(
                f"path '{path}' escapes the allowed root directory"
            )
        return resolved

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    # -- schema ------------------------------------------------------------

    @property
    def tool_names(self) -> set[str]:
        names = {"view", "create_file", "str_replace", "list_directory", "grep"}
        if self.allow_shell:
            names.add("run_shell")
        if self.allow_network:
            names.add("web_fetch")
            names.add("web_search")
        return names

    def schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI-style function schemas for ``bind_tools``."""

        def fn(name: str, description: str, params: dict) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": params,
                },
            }

        schemas: list[dict[str, Any]] = [
            fn(
                "view",
                "Read a UTF-8 text file and return its contents. Paths are "
                "relative to the working directory. Optionally pass a line "
                "range to read only part of a large file.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to read."},
                        "start_line": {"type": "integer", "description": "1-based first line (optional)."},
                        "end_line": {"type": "integer", "description": "Inclusive last line (optional)."},
                    },
                    "required": ["path"],
                },
            ),
            fn(
                "create_file",
                "Create a new file or overwrite an existing one with the given "
                "UTF-8 text content. Parent directories are created as needed.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to write."},
                        "content": {"type": "string", "description": "Full file content."},
                    },
                    "required": ["path", "content"],
                },
            ),
            fn(
                "str_replace",
                "Replace an exact substring in a file. old_str must appear "
                "exactly once; otherwise the edit is rejected so you can add "
                "more surrounding context to disambiguate.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to edit."},
                        "old_str": {"type": "string", "description": "Exact text to replace (must be unique)."},
                        "new_str": {"type": "string", "description": "Replacement text."},
                    },
                    "required": ["path", "old_str", "new_str"],
                },
            ),
            fn(
                "list_directory",
                "List the entries of a directory (relative to the working "
                "directory). Directories are suffixed with '/'.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path (default '.')."},
                    },
                },
            ),
            fn(
                "grep",
                "Search files under the working directory for a regular "
                "expression and return matching lines with file:line prefixes.",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Python regular expression."},
                        "path": {"type": "string", "description": "Subdirectory or file to search (default '.')."},
                        "max_results": {"type": "integer", "description": "Max matching lines (default 100)."},
                    },
                    "required": ["pattern"],
                },
            ),
        ]
        if self.allow_shell:
            schemas.append(
                fn(
                    "run_shell",
                    "Run a shell command in the working directory and return "
                    "its combined stdout/stderr and exit code. Use for builds, "
                    "tests, git, and other CLI operations. Destructive and "
                    "exfiltration commands are blocked.",
                    {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Shell command to execute."},
                            "timeout": {"type": "integer", "description": f"Seconds (default {_DEFAULT_SHELL_TIMEOUT}, max {_MAX_SHELL_TIMEOUT})."},
                        },
                        "required": ["command"],
                    },
                )
            )
        if self.allow_network:
            schemas.append(
                fn(
                    "web_fetch",
                    "Fetch a public HTTP(S) URL and return the response body as "
                    "text. Use for calling REST/JSON APIs and reading web pages. "
                    "Private, loopback, and cloud-metadata addresses are blocked.",
                    {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "http(s) URL to fetch."},
                            "method": {"type": "string", "description": "GET or POST (default GET)."},
                            "headers": {"type": "object", "description": "Optional request headers."},
                            "body": {"type": "string", "description": "Optional request body for POST."},
                            "timeout": {"type": "integer", "description": f"Seconds (default {_DEFAULT_FETCH_TIMEOUT}, max {_MAX_FETCH_TIMEOUT})."},
                        },
                        "required": ["url"],
                    },
                )
            )
            schemas.append(
                fn(
                    "web_search",
                    "Search the web for a query and return a ranked list of "
                    "result titles, URLs, and snippets. Use this to discover "
                    "relevant pages, then call web_fetch on a result URL to read "
                    "its full contents.",
                    {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query."},
                            "max_results": {"type": "integer", "description": "Max results to return (default 5, max 10)."},
                        },
                        "required": ["query"],
                    },
                )
            )
        return schemas

    # -- execution ---------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a built-in tool. Mirrors ``MCPToolBridge.call_tool``."""
        args = arguments or {}
        try:
            if tool_name == "view":
                return self._view(args)
            if tool_name == "create_file":
                return self._create_file(args)
            if tool_name == "str_replace":
                return self._str_replace(args)
            if tool_name == "list_directory":
                return self._list_directory(args)
            if tool_name == "grep":
                return self._grep(args)
            if tool_name == "run_shell":
                if not self.allow_shell:
                    return "Error: run_shell is disabled in this session."
                return await self._run_shell(args)
            if tool_name == "web_fetch":
                if not self.allow_network:
                    return "Error: web_fetch is disabled in this session."
                return await self._web_fetch(args)
            if tool_name == "web_search":
                if not self.allow_network:
                    return "Error: web_search is disabled in this session."
                return await self._web_search(args)
            return f"Error calling tool {tool_name}: unknown built-in tool"
        except ValueError as e:
            return f"Error calling tool {tool_name}: {e}"
        except Exception as e:  # noqa: BLE001 - surface error text to the model
            logger.warning("Built-in tool %s failed: %s", tool_name, e)
            return f"Error calling tool {tool_name}: {e}"

    # -- file tools --------------------------------------------------------

    def _view(self, args: dict) -> str:
        p = self._resolve(args.get("path", ""))
        if p.is_dir():
            return self._list_directory(args)
        if not p.exists():
            return f"Error: file not found: {self._rel(p)}"
        data = p.read_bytes()[:_MAX_READ_BYTES]
        text = data.decode("utf-8", errors="replace")
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None or end is not None:
            lines = text.splitlines()
            s = max(1, int(start or 1))
            e = min(len(lines), int(end or len(lines)))
            numbered = [f"{i}\t{lines[i - 1]}" for i in range(s, e + 1)]
            return _truncate("\n".join(numbered), self.max_output)
        return _truncate(text, self.max_output)

    def _create_file(self, args: dict) -> str:
        p = self._resolve(args.get("path", ""))
        content = args.get("content")
        if content is None:
            raise ValueError("content is required")
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(str(content), encoding="utf-8")
        verb = "Overwrote" if existed else "Created"
        return f"{verb} {self._rel(p)} ({len(str(content))} chars)."

    def _str_replace(self, args: dict) -> str:
        p = self._resolve(args.get("path", ""))
        old = args.get("old_str")
        new = args.get("new_str")
        if old is None or new is None:
            raise ValueError("old_str and new_str are required")
        if not p.exists():
            return f"Error: file not found: {self._rel(p)}"
        text = p.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            return f"Error: old_str not found in {self._rel(p)}."
        if count > 1:
            return (
                f"Error: old_str appears {count} times in {self._rel(p)}; "
                "add more surrounding context so it is unique."
            )
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"Edited {self._rel(p)}."

    def _list_directory(self, args: dict) -> str:
        p = self._resolve(args.get("path", ".") or ".")
        if not p.exists():
            return f"Error: directory not found: {self._rel(p)}"
        if not p.is_dir():
            return f"Error: not a directory: {self._rel(p)}"
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
            entries.append(child.name + ("/" if child.is_dir() else ""))
        listing = "\n".join(entries) if entries else "(empty)"
        return _truncate(f"{self._rel(p)}:\n{listing}", self.max_output)

    def _grep(self, args: dict) -> str:
        pattern = args.get("pattern")
        if not pattern:
            raise ValueError("pattern is required")
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regex: {e}"
        base = self._resolve(args.get("path", ".") or ".")
        max_results = min(int(args.get("max_results", 100) or 100), 1000)
        results: list[str] = []
        targets = [base] if base.is_file() else base.rglob("*")
        for f in targets:
            if len(results) >= max_results:
                break
            if not f.is_file():
                continue
            try:
                for n, line in enumerate(
                    f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if regex.search(line):
                        results.append(f"{self._rel(f)}:{n}: {line.strip()[:300]}")
                        if len(results) >= max_results:
                            break
            except (OSError, ValueError):
                continue
        if not results:
            return "No matches found."
        return _truncate("\n".join(results), self.max_output)

    # -- shell -------------------------------------------------------------

    async def _run_shell(self, args: dict) -> str:
        command = args.get("command")
        if not command or not isinstance(command, str):
            raise ValueError("command is required")
        for regex, label in _SHELL_BLOCKLIST:
            if regex.search(command):
                return f"Error: command blocked ({label}). Refusing to run: {command!r}"
        timeout = min(int(args.get("timeout", _DEFAULT_SHELL_TIMEOUT) or _DEFAULT_SHELL_TIMEOUT), _MAX_SHELL_TIMEOUT)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.root),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Error: command timed out after {timeout}s: {command!r}"
        output = (stdout or b"").decode("utf-8", errors="replace")
        header = f"$ {command}\n(exit code {proc.returncode})\n"
        return _truncate(header + output, self.max_output)

    # -- network -----------------------------------------------------------

    async def _web_fetch(self, args: dict) -> str:
        import httpx

        url = args.get("url")
        if not url or not isinstance(url, str):
            raise ValueError("url is required")
        method = str(args.get("method", "GET") or "GET").upper()
        if method not in {"GET", "POST"}:
            return "Error: only GET and POST are supported."
        headers = args.get("headers") if isinstance(args.get("headers"), dict) else None
        body = args.get("body")
        timeout = min(int(args.get("timeout", _DEFAULT_FETCH_TIMEOUT) or _DEFAULT_FETCH_TIMEOUT), _MAX_FETCH_TIMEOUT)

        # Manually follow redirects so every hop is SSRF-validated.
        current = url
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _hop in range(_MAX_REDIRECTS + 1):
                try:
                    validate_url(current, purpose="web_fetch")
                except SSRFError as e:
                    return f"Error: blocked URL ({e})."
                resp = await client.request(
                    method,
                    current,
                    headers=headers,
                    content=body if (method == "POST" and body is not None) else None,
                )
                if resp.is_redirect and resp.headers.get("location"):
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    method = "GET"
                    body = None
                    continue
                text = resp.text[:_MAX_READ_BYTES]
                status_line = f"HTTP {resp.status_code} {resp.reason_phrase} ({current})\n"
                return _truncate(status_line + text, self.max_output)
        return f"Error: too many redirects (>{_MAX_REDIRECTS})."

    async def _web_search(self, args: dict) -> str:
        import html as _html
        from urllib.parse import parse_qs, unquote, urlparse

        import httpx

        query = args.get("query")
        if not query or not isinstance(query, str):
            raise ValueError("query is required")
        max_results = min(max(int(args.get("max_results", 5) or 5), 1), 10)

        # DuckDuckGo HTML endpoint — no API key required, so web search works
        # out of the box. The endpoint host is public; result URLs are returned
        # as text (not fetched here), so there is no SSRF concern.
        endpoint = "https://html.duckduckgo.com/html/"
        validate_url(endpoint, purpose="web_search")
        try:
            async with httpx.AsyncClient(
                timeout=_DEFAULT_FETCH_TIMEOUT, follow_redirects=True
            ) as client:
                resp = await client.post(
                    endpoint,
                    data={"q": query},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; Lucent/1.0)"},
                )
        except httpx.HTTPError as e:
            return f"Error: web search request failed: {e}"

        body = resp.text
        # Result anchors: <a class="result__a" href="...">title</a>
        anchor = re.compile(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        snippet = re.compile(
            r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        tags = re.compile(r"<[^>]+>")

        def clean(s: str) -> str:
            return _html.unescape(tags.sub("", s)).strip()

        def resolve(href: str) -> str:
            # DDG wraps results in /l/?uddg=<encoded-target>; unwrap it.
            href = _html.unescape(href)
            if href.startswith("//"):
                href = "https:" + href
            parsed = urlparse(href)
            if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
                target = parse_qs(parsed.query).get("uddg")
                if target:
                    return unquote(target[0])
            return href

        hrefs = anchor.findall(body)
        snippets = snippet.findall(body)
        if not hrefs:
            return f"No web results found for {query!r}."

        lines: list[str] = [f"Web search results for {query!r}:"]
        for i, (href, title) in enumerate(hrefs[:max_results]):
            url = resolve(href)
            title_text = clean(title) or url
            line = f"\n{i + 1}. {title_text}\n   {url}"
            if i < len(snippets):
                snip = clean(snippets[i])
                if snip:
                    line += f"\n   {snip}"
            lines.append(line)
        return _truncate("\n".join(lines), self.max_output)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "off", "no", ""}


def build_default_toolset(*, approve_permissions: bool = True) -> BuiltinToolset | None:
    """Build the engine's built-in toolset from environment settings.

    Returns ``None`` when built-in tools are disabled, or when
    ``approve_permissions`` is False (restricted web chat, which must use only
    explicitly configured MCP tools — mirroring the Copilot engine's
    ``excluded_tools`` behavior).
    """
    if not approve_permissions:
        return None
    if not _env_flag("LUCENT_LANGCHAIN_BUILTIN_TOOLS", True):
        return None
    return BuiltinToolset(
        root_dir=os.environ.get("LUCENT_LANGCHAIN_TOOLS_ROOT") or None,
        allow_shell=_env_flag("LUCENT_LANGCHAIN_ALLOW_SHELL", True),
        allow_network=_env_flag("LUCENT_LANGCHAIN_ALLOW_NETWORK", True),
    )
