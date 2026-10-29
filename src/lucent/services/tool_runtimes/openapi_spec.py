"""OpenAPI spec parsing shared by the ``openapi`` runtime and the UI.

Vendors converge on this transform: Bedrock's ``apiSchema`` action groups, Google
ADK's ``OpenAPIToolset``, AWS AgentCore targets, and most MCP server generators
all compile an OpenAPI document into a set of callable operations. Lucent does
the same thing, but keeps the *definition* per operation so the existing
capability/consent envelope applies unchanged.

Scope is intentionally a useful subset, not the whole specification:

* OpenAPI 3.x (``openapi: 3.0.0``+) and Swagger 2.0 (``swagger: "2.0"``).
* Server selection from ``servers[]`` (3.x) or ``schemes``/``host``/``basePath``
  (2.0), with variable defaults substituted.
* Path-level and operation-level ``parameters`` (path, query, header).
* ``requestBody`` (3.x) and ``parameters`` with ``in: body`` (2.0), JSON only.
* ``$ref`` resolution restricted to ``#/components/...`` and ``#/definitions/...``
  in the same document.

What it refuses, because accepting it would create a tool Lucent cannot honestly
describe to a model or safely enforce:

* remote ``$ref`` targets (an SSRF vector and an unbounded include);
* ``callbacks``, ``links``, webhooks (they describe inbound traffic, not a call);
* non-JSON request bodies other than explicit form encoding.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from lucent.logging import get_logger

logger = get_logger("services.tool_runtimes.openapi_spec")

__all__ = [
    "SpecError",
    "derive_input_schema",
    "list_operations",
    "parse_spec",
    "resolve_operation",
]

_MAX_OPERATIONS = 200
_MAX_REF_DEPTH = 12
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head")

# Identifier-safe tool names, matching ``managed_tool_definitions.name``.
_OPERATION_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


class SpecError(ValueError):
    """An OpenAPI document is malformed or uses an unsupported construct."""


def parse_spec(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Parse and minimally validate an OpenAPI/Swagger document."""
    if isinstance(raw, str):
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            # YAML is extremely common; only attempt it when the JSON parse fails
            # and PyYAML is available (it is a base dependency).
            try:
                import yaml

                document = yaml.safe_load(raw)
            except Exception:
                raise SpecError(f"OpenAPI document is not valid JSON or YAML: {exc}") from exc
    else:
        document = raw
    if not isinstance(document, dict):
        raise SpecError("OpenAPI document must be a mapping at the top level")

    version = document.get("openapi")
    swagger = document.get("swagger")
    if version is None and swagger is None:
        raise SpecError("Document has neither an 'openapi' nor a 'swagger' version field")
    if version is not None and not str(version).startswith("3."):
        raise SpecError(f"Unsupported OpenAPI version {version!r}; Lucent supports 3.x")
    if version is None and str(swagger) != "2.0":
        raise SpecError(f"Unsupported Swagger version {swagger!r}; Lucent supports 2.0")

    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise SpecError("Document declares no 'paths'")
    if not _has_operations(paths):
        raise SpecError("Document declares no operations under 'paths'")
    return document


def _has_operations(paths: dict[str, Any]) -> bool:
    for item in paths.values():
        if not isinstance(item, dict):
            continue
        if any(method in item for method in _HTTP_METHODS):
            return True
    return False


def _is_swagger_2(document: dict[str, Any]) -> bool:
    return str(document.get("swagger") or "").strip() == "2.0"


def _resolve_ref(document: dict[str, Any], ref: str, depth: int = 0) -> Any:
    """Resolve a local ``$ref`` only. Remote refs are refused."""
    if depth > _MAX_REF_DEPTH:
        raise SpecError(f"$ref nesting exceeded {_MAX_REF_DEPTH} levels")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise SpecError(
            f"Only local $ref values are supported; refusing {ref!r}. Inline the "
            "referenced schema instead."
        )
    node: Any = document
    for raw_segment in ref[2:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or segment not in node:
            raise SpecError(f"$ref {ref!r} does not resolve inside this document")
        node = node[segment]
    return node


def _deref(document: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """Recursively dereference ``$ref`` occurrences inside a node."""
    if depth > _MAX_REF_DEPTH:
        raise SpecError(f"$ref nesting exceeded {_MAX_REF_DEPTH} levels")
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            return _deref(document, _resolve_ref(document, ref, depth + 1), depth + 1)
        return {key: _deref(document, value, depth + 1) for key, value in node.items()}
    if isinstance(node, list):
        return [_deref(document, item, depth + 1) for item in node]
    return node


def _base_url(document: dict[str, Any]) -> str:
    """Pick the base URL. Returns ``""`` when the document declares none."""
    if _is_swagger_2(document):
        host = document.get("host")
        base_path = str(document.get("basePath") or "")
        schemes = document.get("schemes") or []
        if not isinstance(host, str) or not host:
            return ""
        scheme = "https"
        if isinstance(schemes, list) and schemes:
            # Prefer HTTPS even when the spec lists both, so a document written
            # before TLS was universal does not silently downgrade transport.
            scheme = "https" if "https" in schemes else str(schemes[0])
        return f"{scheme}://{host}{base_path}".rstrip("/")

    servers = document.get("servers")
    if not isinstance(servers, list) or not servers:
        return ""
    first = servers[0]
    if not isinstance(first, dict):
        return ""
    url = first.get("url")
    if not isinstance(url, str) or not url:
        return ""
    variables = first.get("variables")
    if isinstance(variables, dict):
        for name, spec in variables.items():
            if isinstance(spec, dict) and "default" in spec:
                url = url.replace(f"{{{name}}}", str(spec["default"]))
    return url.rstrip("/")


def list_operations(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Enumerate callable operations with their identifiers and summaries."""
    if not isinstance(document, dict):
        raise SpecError("OpenAPI document must be a mapping")
    base_url = _base_url(document)
    swagger = _is_swagger_2(document)
    operations: list[dict[str, Any]] = []

    paths = document.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id.strip():
                operation_id = f"{method}_{path}"
            name = _sanitize_name(operation_id)
            operations.append(
                {
                    "operation_id": str(operation_id),
                    "name": name,
                    "method": method.upper(),
                    "path": str(path),
                    "summary": operation.get("summary") or operation.get("description") or "",
                    "deprecated": bool(operation.get("deprecated", False)),
                    "base_url": base_url,
                    "spec_kind": "swagger2" if swagger else "openapi3",
                }
            )
            if len(operations) >= _MAX_OPERATIONS:
                logger.warning(
                    "OpenAPI document has more than %s operations; truncating the list. "
                    "Create per-operation tools for the ones you need.",
                    _MAX_OPERATIONS,
                )
                return operations
    return operations


def _sanitize_name(value: str) -> str:
    cleaned = _OPERATION_NAME_RE.sub("-", value).strip("-_").lower()
    return (cleaned or "operation")[:64]


def resolve_operation(
    document: dict[str, Any],
    *,
    operation_id: str | None = None,
    method: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Locate one operation by id, or by explicit method + path."""
    operations = list_operations(document)
    if operation_id:
        for candidate in operations:
            if candidate["operation_id"] == operation_id or candidate["name"] == operation_id:
                return candidate
        raise SpecError(f"No operation with id {operation_id!r} in this document")

    if method and path:
        wanted_method = method.strip().upper()
        for candidate in operations:
            if candidate["method"] == wanted_method and candidate["path"] == path:
                return candidate
        raise SpecError(f"No {wanted_method} operation at {path!r} in this document")

    raise SpecError("Specify operation_id, or both method and path")


def _path_item(document: dict[str, Any], path: str) -> dict[str, Any]:
    paths = document.get("paths") or {}
    item = paths.get(path)
    if not isinstance(item, dict):
        raise SpecError(f"Spec declares no path {path!r}")
    return item


def derive_input_schema(
    document: dict[str, Any],
    *,
    operation_id: str | None = None,
    method: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Build the model-facing ``input_schema`` for one operation.

    Path and query parameters become top-level object properties, flattened by
    their own name. A JSON request body's object properties are merged in at the
    top level too, because a model calling one tool has no use for the
    ``{path, query, body}`` envelope — that is an HTTP detail. Collisions are
    resolved in favour of path parameters (which are always required) and are
    reported as a spec error rather than silently renamed.
    """
    operation = resolve_operation(
        document, operation_id=operation_id, method=method, path=path
    )
    path_item = _path_item(document, operation["path"])
    operation_node = path_item.get(operation["method"].lower())
    if not isinstance(operation_node, dict):
        raise SpecError("Operation node disappeared while deriving its schema")

    properties: dict[str, Any] = {}
    required: list[str] = []
    collisions: list[str] = []

    def _add(name: str, schema: dict[str, Any], is_required: bool) -> None:
        if name in properties:
            collisions.append(name)
            if is_required:
                # A required parameter wins the name; the body field becomes
                # inaccessible, which is why this is reported below.
                properties[name] = schema
        else:
            properties[name] = schema
        if is_required and name not in required:
            required.append(name)

    path_parameters = (
        path_item.get("parameters")
        if isinstance(path_item.get("parameters"), list)
        else []
    )
    operation_parameters = (
        operation_node.get("parameters")
        if isinstance(operation_node.get("parameters"), list)
        else []
    )
    # Operation-level parameters override path-level ones with the same
    # (name, in) pair, per the specification's merge rule.
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in [*path_parameters, *operation_parameters]:
        parameter = _deref(document, raw)
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        if not isinstance(name, str) or not isinstance(location, str):
            continue
        merged[(name, location)] = parameter

    for (name, location), parameter in merged.items():
        if location == "cookie":
            # Cookies would carry ambient credentials the definition did not
            # declare; refuse rather than silently drop the parameter.
            raise SpecError(
                f"Cookie parameter {name!r} is not supported by Lucent OpenAPI tools"
            )
        if location == "body":
            continue  # Swagger 2.0 body handled below
        schema = _deref(document, parameter.get("schema") or {})
        if not isinstance(schema, dict):
            schema = {}
        if "description" not in schema and parameter.get("description"):
            schema = {**schema, "description": parameter["description"]}
        is_required = bool(parameter.get("required")) or location == "path"
        _add(name, schema, is_required)

    if _is_swagger_2(document):
        body_parameter = next(
            (
                parameter
                for parameter in (
                    _deref(document, raw) for raw in [*path_parameters, *operation_parameters]
                )
                if isinstance(parameter, dict) and parameter.get("in") == "body"
            ),
            None,
        )
        body_schema = _deref(document, (body_parameter or {}).get("schema") or {})
        if isinstance(body_schema, dict) and body_schema.get("type") not in (None, "object"):
            _add("body", body_schema, True)
        elif isinstance(body_schema, dict) and isinstance(body_schema.get("properties"), dict):
            body_required = body_schema.get("required") or []
            for prop_name, prop_schema in body_schema["properties"].items():
                _add(
                    str(prop_name),
                    _deref(document, prop_schema) if isinstance(prop_schema, dict) else {},
                    str(prop_name) in (body_required or []),
                )
    else:
        request_body = _deref(document, operation_node.get("requestBody") or {})
        if isinstance(request_body, dict):
            content = request_body.get("content") or {}
            if isinstance(content, dict) and content:
                media_type = _preferred_media_type(content)
                if media_type is None:
                    raise SpecError(
                        "requestBody declares no JSON media type; Lucent OpenAPI tools "
                        "support application/json and form encodings"
                    )
                schema = _deref(document, (content.get(media_type) or {}).get("schema") or {})
                if isinstance(schema, dict):
                    body_properties = schema.get("properties")
                    body_required = schema.get("required") or []
                    if isinstance(body_properties, dict):
                        for prop_name, prop_schema in body_properties.items():
                            _add(
                                str(prop_name),
                                (
                                    _deref(document, prop_schema)
                                    if isinstance(prop_schema, dict)
                                    else {}
                                ),
                                str(prop_name) in (body_required or []),
                            )
                    elif schema.get("type") not in (None, "object"):
                        # A non-object body cannot be flattened; expose it whole.
                        _add(
                            "body",
                            schema,
                            bool(request_body.get("required")),
                        )

    if collisions:
        raise SpecError(
            "This operation has parameter/body field name collisions that cannot be "
            "represented as one flat tool input: " + ", ".join(sorted(set(collisions)))
            + ". Create the tool with a narrower input_schema and an 'http' runtime instead."
        )

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _preferred_media_type(content: dict[str, Any]) -> str | None:
    # ``multipart/form-data`` is deliberately absent: the runtime cannot build a
    # multipart body, so accepting it would send JSON with the wrong Content-Type.
    for candidate in ("application/json", "application/x-www-form-urlencoded"):
        if candidate in content:
            return candidate
    for key in content:
        if str(key).endswith("+json"):
            return str(key)
    return None


def build_request(
    document: dict[str, Any],
    operation: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Map flat arguments onto the operation's path/query/header/body slots."""
    path_item = _path_item(document, operation["path"])
    operation_node = path_item.get(operation["method"].lower()) or {}
    path_parameters = (
        path_item.get("parameters")
        if isinstance(path_item.get("parameters"), list)
        else []
    )
    operation_parameters = (
        operation_node.get("parameters")
        if isinstance(operation_node.get("parameters"), list)
        else []
    )

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in [*path_parameters, *operation_parameters]:
        parameter = _deref(document, raw)
        if isinstance(parameter, dict) and isinstance(parameter.get("name"), str):
            merged[(parameter["name"], str(parameter.get("in")))] = parameter

    path_values: dict[str, str] = {}
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    form: dict[str, str] = {}
    consumed: set[str] = set()

    for (name, location), parameter in merged.items():
        if name not in arguments:
            if location == "path" or parameter.get("required"):
                raise SpecError(
                    f"Required parameter {name!r} was not supplied for this operation"
                )
            continue
        value = arguments[name]
        if location == "path":
            path_values[name] = _path_segment(value, name)
            consumed.add(name)
        elif location == "query":
            if isinstance(value, list):
                query[name] = [_scalarize(item, name) for item in value]
            else:
                query[name] = _scalarize(value, name)
            consumed.add(name)
        elif location == "header":
            headers[name] = _scalarize(value, name)
            consumed.add(name)
        elif location == "formData":
            # Swagger 2.0 form uploads. Without this branch every formData
            # operation was advertised in the schema and then rejected as
            # "arguments do not map to any parameter".
            if isinstance(value, list):
                form[name] = ",".join(_scalarize(item, name) for item in value)
            else:
                form[name] = _scalarize(value, name)
            consumed.add(name)

    body: Any = None
    form_body: dict[str, str] | None = None
    media_type = "application/json"
    declares_flat_object = False

    if _is_swagger_2(document):
        consumes = document.get("consumes") or []
        if not isinstance(consumes, list):
            consumes = []
        for (name, location), parameter in merged.items():
            if location != "body":
                continue
            body_schema = _deref(document, parameter.get("schema") or {})
            if isinstance(body_schema, dict) and body_schema.get("type") not in (
                None, "object"
            ):
                # A non-object Swagger body is exposed as a single `body`
                # property (see derive_input_schema), so read it whole rather
                # than wrapping it in an object keyed by nothing.
                if "body" in arguments:
                    body = arguments["body"]
                    consumed.add("body")
            else:
                declares_flat_object = True
                body = {
                    key: value for key, value in arguments.items() if key not in consumed
                }
                consumed.update(body)
            break
        if "multipart/form-data" in consumes:
            media_type = "multipart/form-data"
        elif "application/x-www-form-urlencoded" in consumes:
            media_type = "application/x-www-form-urlencoded"
    else:
        request_body = _deref(document, operation_node.get("requestBody") or {})
        content = (request_body or {}).get("content") if isinstance(request_body, dict) else None
        if isinstance(content, dict) and content:
            chosen = _preferred_media_type(content)
            if chosen:
                media_type = chosen
                schema = _deref(document, (content.get(chosen) or {}).get("schema") or {})
                if isinstance(schema, dict) and schema.get("type") not in (None, "object"):
                    if "body" in arguments:
                        body = arguments["body"]
                        consumed.add("body")
                else:
                    declares_flat_object = True
                    body = {
                        key: value for key, value in arguments.items() if key not in consumed
                    }
                    consumed.update(body)

    if form:
        if form_body is not None:
            form_body.update(form)
        else:
            form_body = dict(form)
        if media_type == "application/json":
            media_type = "application/x-www-form-urlencoded"

    unused = set(arguments) - consumed
    if unused:
        raise SpecError(
            "Arguments do not map to any parameter of this operation: "
            + ", ".join(sorted(unused))
        )

    base = operation.get("base_url") or _base_url(document)
    if not base:
        raise SpecError(
            "This document declares no server URL. Add a 'servers' entry (OpenAPI 3.x) "
            "or 'host' + 'schemes' (Swagger 2.0), or set runtime_config.base_url."
        )

    path = operation["path"]
    for name, value in path_values.items():
        path = path.replace(f"{{{name}}}", value)

    return {
        "base_url": base,
        "path": path,
        "method": operation["method"],
        "query": query,
        "headers": headers,
        "body": body,
        "form_body": form_body,
        "media_type": media_type,
        "flattened_body": declares_flat_object,
    }


def _path_segment(value: Any, name: str) -> str:
    """Percent-encode a path parameter and refuse traversal segments.

    A model-supplied path value is the single most dangerous substitution in an
    OpenAPI tool: unencoded, ``../../admin`` rewrites the operation and ``x?y=1``
    injects a query. Encoding alone is not enough because many servers decode
    ``%2F`` before routing, so a bare ``..`` segment is rejected outright.
    """
    text = _scalarize(value, name)
    if text in (".", ".."):
        raise SpecError(f"Path parameter {name!r} must not be a relative path segment")
    encoded = quote(text, safe="")
    # Belt and braces: encode the dots in a traversal-shaped value so a server
    # that decodes %2F still cannot walk upward.
    if encoded and set(encoded.replace("%2E", ".")) == {"."}:
        encoded = encoded.replace(".", "%2E")
    return encoded


def _scalarize(value: Any, name: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise SpecError(
        f"Parameter {name!r} must be a scalar for this operation; received "
        f"{type(value).__name__}"
    )


def join_url(base_url: str, path: str) -> str:
    """Join a server base URL and an operation path without duplicating slashes."""
    base = base_url.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    joined = f"{base}{suffix}"
    # Guard against a spec that put an absolute URL in `path`.
    if urlsplit(joined).scheme not in ("http", "https"):
        raise SpecError(f"Operation path produced a non-HTTP URL: {joined!r}")
    return joined


def operation_document_from_spec_text(raw: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convenience for the UI: parse once, return document and operation list."""
    document = parse_spec(raw)
    return document, list_operations(document)


def normalize_base_url_for_storage(base_url: str) -> str:
    """Validate an author-supplied base URL before it is stored."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise SpecError("base_url must be a non-empty string")
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SpecError("base_url must be an absolute http(s) URL")
    return base_url.strip()


def absolute_operation_url(base_url: str, path: str) -> str:
    """``urljoin``-style combination used when a spec path is already absolute."""
    if urlsplit(path).scheme in ("http", "https"):
        return path
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
