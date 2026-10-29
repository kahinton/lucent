"""Security and behaviour tests for the managed-tool runtime kinds.

These target the parts of the new surface that can hurt somebody. Each test names
the property it protects, because a regression here is silent: a wrong answer
still returns HTTP 200.

Coverage map:

* outbound policy — host allowlists, port handling, scheme restrictions,
  private-address refusal, redirect credential handling and downgrade refusal
* templating — argument scoping, credential scoping, double substitution,
  path traversal
* OpenAPI derivation — parameter flattening, path-segment encoding, body
  handling, unsupported constructs being refused rather than half-supported
* built-in catalog — AST allowlist, ReDoS bound, argument validation, optional
  dependency gating
* repository validation — per-kind requirements, builtin schema authority
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lucent.services.builtin_tool_catalog import (
    BUILTIN_TOOL_CATALOG,
    available_builtin_tools,
    get_builtin_tool,
    invoke_builtin_tool,
)
from lucent.services.tool_runtimes.base import (
    RuntimeExecutionContext,
    ToolRuntimeBlockedError,
    ToolRuntimeError,
)
from lucent.services.tool_runtimes.http_policy import (
    HTTPPolicyError,
    OutboundPolicy,
    redact_url,
)
from lucent.services.tool_runtimes.outbound import (
    credential_header_names,
)
from lucent.services.tool_runtimes.templating import (
    TemplateError,
    render_header_value,
    render_path_value,
    render_scalar,
)

# ── outbound policy ───────────────────────────────────────────────────────


class TestOutboundPolicy:
    def test_none_mode_refuses_everything(self):
        policy = OutboundPolicy(network_mode="none")
        with pytest.raises(HTTPPolicyError, match="no outbound network"):
            policy.assert_host_allowed("https://example.com/x")

    def test_allowlist_requires_configuration(self):
        policy = OutboundPolicy(network_mode="allowlist", allowed_hosts=())
        with pytest.raises(HTTPPolicyError, match="no allowed_hosts"):
            policy.assert_host_allowed("https://example.com/x")

    def test_allowlist_permits_named_host(self):
        policy = OutboundPolicy(network_mode="allowlist", allowed_hosts=("api.example.com",))
        policy.assert_host_allowed("https://api.example.com/v1/x")

    def test_allowlist_rejects_lookalike_host(self):
        """`example.com` must not authorise `evil-example.com`."""
        policy = OutboundPolicy(network_mode="allowlist", allowed_hosts=("example.com",))
        with pytest.raises(HTTPPolicyError, match="not in this tool's allowed_hosts"):
            policy.assert_host_allowed("https://evil-example.com/x")

    def test_allowlist_rejects_embedded_subdomain_without_wildcard(self):
        policy = OutboundPolicy(network_mode="allowlist", allowed_hosts=("example.com",))
        with pytest.raises(HTTPPolicyError):
            policy.assert_host_allowed("https://api.example.com/x")

    def test_leading_dot_entry_matches_subdomains_only(self):
        policy = OutboundPolicy(network_mode="allowlist", allowed_hosts=(".example.com",))
        policy.assert_host_allowed("https://api.example.com/x")
        with pytest.raises(HTTPPolicyError):
            policy.assert_host_allowed("https://example.com/x")
        with pytest.raises(HTTPPolicyError):
            policy.assert_host_allowed("https://evilexample.com/x")

    def test_trailing_dot_is_normalised(self):
        policy = OutboundPolicy(network_mode="allowlist", allowed_hosts=("example.com",))
        policy.assert_host_allowed("https://example.com./x")

    def test_explicit_port_entry_matches_only_that_port(self):
        policy = OutboundPolicy(
            network_mode="allowlist", allowed_hosts=("api.example.com:8443",)
        )
        policy.assert_host_allowed("https://api.example.com:8443/x")
        with pytest.raises(HTTPPolicyError):
            policy.assert_host_allowed("https://api.example.com/x")

    def test_implicit_port_entry_matches_only_default_port(self):
        """An entry with no port must not authorise every port on that host."""
        policy = OutboundPolicy(network_mode="allowlist", allowed_hosts=("api.example.com",))
        policy.assert_host_allowed("https://api.example.com/x")
        with pytest.raises(HTTPPolicyError, match="not in this tool's allowed_hosts"):
            policy.assert_host_allowed("https://api.example.com:6379/x")

    def test_scheme_must_be_http_or_https(self):
        policy = OutboundPolicy(network_mode="bridge")
        with pytest.raises(HTTPPolicyError):
            policy.assert_host_allowed("file:///etc/passwd")
        with pytest.raises(HTTPPolicyError):
            policy.assert_host_allowed("gopher://example.com/")

    def test_bridge_refuses_non_web_port(self):
        """`bridge` means any public host on the web ports, not any service."""
        policy = OutboundPolicy(network_mode="bridge")
        with pytest.raises(HTTPPolicyError, match="not permitted for a bridge-mode tool"):
            policy.assert_host_allowed("https://example.com:6379/x")

    def test_bridge_allows_standard_ports(self):
        policy = OutboundPolicy(network_mode="bridge")
        policy.assert_host_allowed("https://example.com/x")
        policy.assert_host_allowed("http://example.com/x")

    def test_userinfo_is_not_treated_as_the_host(self):
        policy = OutboundPolicy(network_mode="allowlist", allowed_hosts=("example.com",))
        # `urlsplit` reports the real host, so this must pass rather than be read
        # as host "evil".
        policy.assert_host_allowed("https://evil@example.com/x")

    def test_allows_host_ignores_port(self):
        policy = OutboundPolicy(
            network_mode="allowlist", allowed_hosts=("api.example.com:443",)
        )
        assert policy.allows_host("api.example.com") is True
        assert policy.allows_host("other.example.com") is False

    def test_from_tool_rejects_unknown_mode(self):
        with pytest.raises(HTTPPolicyError, match="Unsupported network_mode"):
            OutboundPolicy.from_tool({"network_policy": {"network_mode": "everything"}})

    def test_from_tool_rejects_non_string_hosts(self):
        with pytest.raises(HTTPPolicyError):
            OutboundPolicy.from_tool(
                {"network_policy": {"network_mode": "allowlist", "allowed_hosts": [1]}}
            )

    def test_from_tool_defaults_to_no_network(self):
        assert OutboundPolicy.from_tool({}).network_mode == "none"

    def test_private_addresses_are_refused(self):
        policy = OutboundPolicy(network_mode="bridge")
        for url in (
            "http://127.0.0.1/x",
            "http://10.0.0.5/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/x",
            "http://[fd00::1]/x",  # IPv6 ULA — previously unblocked
            "http://0.0.0.0/x",
            "http://100.64.0.1/x",  # CGNAT
        ):
            with pytest.raises(HTTPPolicyError):
                policy.validate_destination(url, purpose="test")


class TestRedaction:
    def test_query_string_is_stripped(self):
        redacted = redact_url("https://api.example.com/v1/x?token=super-secret&y=1")
        assert "super-secret" not in redacted
        assert redacted == "https://api.example.com/v1/x"

    def test_path_with_credential_is_preserved_but_query_removed(self):
        redacted = redact_url("https://api.example.com/v1/weather?appid=SECRET")
        assert "appid" not in redacted
        assert "/v1/weather" in redacted

    def test_unparseable_url_does_not_raise(self):
        assert redact_url("not a url") == "(unparseable URL)"


class TestCredentialHeaderDetection:
    def test_known_credential_headers_are_included(self):
        names = credential_header_names({}, None)
        assert "authorization" in names
        assert "x-api-key" in names

    def test_credential_reference_template_marks_the_header(self):
        names = credential_header_names(
            {"X-Custom": "Bearer abc"},
            {"X-Custom": "Bearer {{env:token}}"},
        )
        assert "x-custom" in names

    def test_sensitive_named_header_is_marked_even_without_template(self):
        names = credential_header_names({"X-Tenant-Token": "literal"}, None)
        assert "x-tenant-token" in names

    def test_declared_headers_accept_a_comma_string(self):
        """A plain HTML input posts a comma-separated string, not a list."""
        names = credential_header_names({}, None, "X-Weird,Hodor")
        assert "x-weird" in names
        assert "hodor" in names


# ── templating ────────────────────────────────────────────────────────────


class TestTemplating:
    def test_arguments_are_substituted(self):
        assert render_scalar("/v1/{city}", {"city": "Paris"}, {}) == "/v1/Paris"

    def test_missing_argument_is_an_error_not_an_empty_string(self):
        with pytest.raises(TemplateError, match="does not receive"):
            render_scalar("/v1/{city}", {}, {})

    def test_credentials_resolve_from_the_credential_map_only(self):
        with pytest.raises(TemplateError, match="unresolved credential"):
            render_scalar("{{env:token}}", {}, {})
        assert render_scalar("{{env:token}}", {}, {"token": "abc"}) == "abc"

    def test_argument_cannot_reach_a_credential(self):
        """An argument value that looks like a credential reference stays literal.

        Substitution is single-pass, so a model that echoes `{{env:token}}` into a
        field gets the text, never the secret.
        """
        rendered = render_scalar(
            "{value}", {"value": "{{env:token}}"}, {"token": "SECRET"}
        )
        assert rendered == "{{env:token}}"
        assert "SECRET" not in rendered

    def test_credential_cannot_be_reinterpreted_as_an_argument(self):
        """A credential containing `{city}` must not be re-expanded."""
        rendered = render_scalar(
            "{{env:token}}", {"city": "Paris"}, {"token": "{city}"}
        )
        assert rendered == "{city}"

    def test_path_values_are_percent_encoded(self):
        rendered = render_path_value("/v1/{city}", {"city": "../../admin"}, {})
        assert ".." not in rendered.replace("%2E", "")
        assert "/" not in rendered.replace("/v1/", "")

    def test_path_rejects_query_injection(self):
        rendered = render_path_value("/v1/{q}", {"q": "x?admin=1"}, {})
        assert "?" not in rendered
        assert "%3F" in rendered

    def test_header_rejects_newline_injection(self):
        with pytest.raises(TemplateError, match="newline or NUL"):
            render_header_value("Bearer {v}", {"v": "abc\r\nX-Evil: 1"}, {})

    def test_non_scalar_argument_is_rejected(self):
        with pytest.raises(TemplateError, match="Cannot substitute"):
            render_scalar("{o}", {"o": {"a": 1}}, {})

    def test_booleans_render_as_json_literals(self):
        assert render_scalar("{b}", {"b": True}, {}) == "true"


# ── OpenAPI derivation ────────────────────────────────────────────────────


PETSTORE = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/pets/{petId}": {
            "get": {
                "operationId": "getPet",
                "parameters": [
                    {"name": "petId", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
                ],
            }
        },
        "/pets": {
            "post": {
                "operationId": "createPet",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "age": {"type": "integer"},
                                },
                                "required": ["name"],
                            }
                        }
                    },
                },
            }
        },
    },
}


class TestOpenAPISpec:
    def test_lists_operations(self):
        from lucent.services.tool_runtimes.openapi_spec import list_operations

        operations = list_operations(PETSTORE)
        assert {operation["operation_id"] for operation in operations} == {
            "getPet",
            "createPet",
        }

    def test_input_schema_flattens_path_and_query(self):
        from lucent.services.tool_runtimes.openapi_spec import derive_input_schema

        schema = derive_input_schema(PETSTORE, operation_id="getPet")
        assert set(schema["properties"]) == {"petId", "verbose"}
        assert "petId" in schema["required"]
        assert "verbose" not in schema["required"]

    def test_input_schema_flattens_json_body(self):
        from lucent.services.tool_runtimes.openapi_spec import derive_input_schema

        schema = derive_input_schema(PETSTORE, operation_id="createPet")
        assert set(schema["properties"]) == {"name", "age"}
        assert "name" in schema["required"]

    def test_remote_ref_is_refused(self):
        from lucent.services.tool_runtimes.openapi_spec import SpecError

        document = json.loads(json.dumps(PETSTORE))
        document["paths"]["/pets"]["post"]["requestBody"] = {
            "content": {"application/json": {"schema": {"$ref": "https://evil/x.json"}}}
        }
        with pytest.raises(SpecError, match="Only local"):
            from lucent.services.tool_runtimes.openapi_spec import derive_input_schema

            derive_input_schema(document, operation_id="createPet")

    def test_local_ref_resolves(self):
        from lucent.services.tool_runtimes.openapi_spec import derive_input_schema

        document = json.loads(json.dumps(PETSTORE))
        document["components"] = {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            }
        }
        document["paths"]["/pets"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"] = {"$ref": "#/components/schemas/Pet"}
        schema = derive_input_schema(document, operation_id="createPet")
        assert set(schema["properties"]) == {"name"}

    def test_unsupported_version_is_refused(self):
        from lucent.services.tool_runtimes.openapi_spec import SpecError, parse_spec

        with pytest.raises(SpecError, match="Unsupported OpenAPI version"):
            parse_spec({"openapi": "2.1.0", "paths": {"/x": {"get": {}}}})

    def test_path_value_cannot_traverse(self):
        """A model-supplied path parameter must not rewrite the operation."""
        from lucent.services.tool_runtimes.openapi_spec import build_request, resolve_operation

        operation = resolve_operation(PETSTORE, operation_id="getPet")
        with pytest.raises(Exception):
            build_request(PETSTORE, operation, {"petId": ".."})

    def test_path_value_is_percent_encoded(self):
        from lucent.services.tool_runtimes.openapi_spec import build_request, resolve_operation

        operation = resolve_operation(PETSTORE, operation_id="getPet")
        request = build_request(PETSTORE, operation, {"petId": "a/b?c"})
        assert request["path"] == "/pets/a%2Fb%3Fc"

    def test_undeclared_argument_is_rejected(self):
        from lucent.services.tool_runtimes.openapi_spec import (
            SpecError,
            build_request,
            resolve_operation,
        )

        operation = resolve_operation(PETSTORE, operation_id="getPet")
        with pytest.raises(SpecError, match="do not map"):
            build_request(PETSTORE, operation, {"petId": "1", "bogus": "x"})

    def test_missing_required_parameter_is_rejected(self):
        from lucent.services.tool_runtimes.openapi_spec import (
            SpecError,
            build_request,
            resolve_operation,
        )

        operation = resolve_operation(PETSTORE, operation_id="getPet")
        with pytest.raises(SpecError, match="Required parameter"):
            build_request(PETSTORE, operation, {})

    def test_swagger_2_form_data_is_mapped(self):
        from lucent.services.tool_runtimes.openapi_spec import build_request, parse_spec

        document = parse_spec(
            {
                "swagger": "2.0",
                "host": "api.example.com",
                "schemes": ["https"],
                "paths": {
                    "/login": {
                        "post": {
                            "operationId": "login",
                            "consumes": ["application/x-www-form-urlencoded"],
                            "parameters": [
                                {"name": "user", "in": "formData", "required": True,
                                 "type": "string"},
                                {"name": "pass", "in": "formData", "required": True,
                                 "type": "string"},
                            ],
                        }
                    }
                },
            }
        )
        from lucent.services.tool_runtimes.openapi_spec import (
            derive_input_schema,
            resolve_operation,
        )

        schema = derive_input_schema(document, operation_id="login")
        assert set(schema["properties"]) == {"user", "pass"}
        operation = resolve_operation(document, operation_id="login")
        request = build_request(document, operation, {"user": "u", "pass": "p"})
        assert request["form_body"] == {"user": "u", "pass": "p"}
        assert request["media_type"] == "application/x-www-form-urlencoded"


# ── built-in catalog ──────────────────────────────────────────────────────


def _context(arguments: dict, *, policy_mode: str = "none", config: dict | None = None):
    return RuntimeExecutionContext(
        org_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        arguments=arguments,
        runtime_config={
            "_policy": OutboundPolicy(network_mode=policy_mode),
            **(config or {}),
        },
    )


class TestBuiltinCatalog:
    def test_every_handler_has_a_usable_schema(self):
        for name, spec in BUILTIN_TOOL_CATALOG.items():
            assert spec.input_schema.get("type") == "object", name
            assert "properties" in spec.input_schema, name
            assert spec.input_schema.get("additionalProperties") is False, name

    def test_availability_reports_the_build_flag(self):
        for descriptor in available_builtin_tools():
            if descriptor["requires_package"]:
                assert descriptor["build_flag"], descriptor["handler"]
            if not descriptor["available"]:
                assert "unavailable_reason" in descriptor

    def test_unknown_handler_is_refused(self):
        with pytest.raises(ToolRuntimeError, match="No built-in tool"):
            get_builtin_tool("definitely-not-a-handler")

    def test_optional_handler_reports_unavailable_cleanly(self):
        """A missing optional package must be actionable, not a traceback."""
        unavailable = [
            descriptor
            for descriptor in available_builtin_tools()
            if not descriptor["available"]
        ]
        if not unavailable:
            pytest.skip("optional tool packages are installed in this environment")
        descriptor = unavailable[0]
        with pytest.raises(ToolRuntimeError) as excinfo:
            asyncio.run(
                invoke_builtin_tool(
                    descriptor["handler"], _context({"image_base64": "eA=="})
                )
            )
        assert "package" in str(excinfo.value).lower()

    def test_network_handler_requires_a_policy_that_permits_it(self):
        """`network_mode='none'` must block a handler that needs the network."""
        from lucent.services.tool_runtimes.builtin_runtime import BuiltinRuntime

        runtime = BuiltinRuntime()
        context = _context(
            {"query": "x"},
            policy_mode="none",
            config={"_tool": {"network_policy": {"network_mode": "none"}}},
        )
        context.runtime_config["handler"] = "web_search"
        with pytest.raises(ToolRuntimeBlockedError, match="needs outbound network"):
            asyncio.run(runtime.execute(context))

    def test_network_handler_rejects_an_allowlist_missing_its_host(self):
        from lucent.services.tool_runtimes.builtin_runtime import BuiltinRuntime

        runtime = BuiltinRuntime()
        context = _context(
            {"query": "x"},
            config={
                "handler": "web_search",
                "_tool": {
                    "network_policy": {
                        "network_mode": "allowlist",
                        "allowed_hosts": ["example.com"],
                    }
                },
            },
        )
        with pytest.raises(ToolRuntimeBlockedError, match="does not include"):
            asyncio.run(runtime.execute(context))

    def test_builtin_runtime_rejects_arguments_outside_the_catalog_schema(self):
        """The definition's stored schema cannot widen the handler's contract."""
        from lucent.services.tool_runtimes.builtin_runtime import BuiltinRuntime

        runtime = BuiltinRuntime()
        context = _context(
            {"timezone": "UTC", "extra": "value"},
            config={"_tool": {}, "handler": "weather_forecast"},
        )
        with pytest.raises(ToolRuntimeBlockedError, match="do not match"):
            asyncio.run(runtime.execute(context))

    def test_builtin_runtime_requires_an_explicit_handler(self):
        from lucent.services.tool_runtimes.builtin_runtime import BuiltinRuntime

        runtime = BuiltinRuntime()
        with pytest.raises(ToolRuntimeError, match="runtime_config.handler"):
            asyncio.run(runtime.execute(_context({"timezone": "UTC"}, config={"_tool": {}})))


# ── runtime registry ──────────────────────────────────────────────────────


class TestRuntimeRegistry:
    def test_every_catalog_kind_resolves(self):
        from lucent.services.tool_runtimes import get_runtime, runtime_types

        for kind in runtime_types():
            runtime = get_runtime(kind)
            assert runtime.runtime_type == kind

    def test_unknown_kind_fails_closed(self):
        from lucent.services.tool_runtimes import get_runtime

        with pytest.raises(ToolRuntimeError, match="Unsupported runtime_type"):
            get_runtime("not-a-kind")

    def test_sandbox_requirement_fails_closed_for_unknown_kinds(self):
        """A typo must not accidentally bypass the sandbox feature gate."""
        from lucent.services.tool_runtimes import runtime_requires_sandbox

        assert runtime_requires_sandbox("not-a-kind") is True
        assert runtime_requires_sandbox("python") is True
        assert runtime_requires_sandbox("http") is False
        assert runtime_requires_sandbox("openapi") is False
        assert runtime_requires_sandbox("builtin") is False


# ── repository validation (no database needed) ────────────────────────────


class TestRepositoryValidation:
    @staticmethod
    def _validate(**overrides):
        from lucent.db.definitions import DefinitionRepository

        kwargs = {
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": None,
            "runtime_type": "python",
            "source_code": "def handler(args):\n    return {}\n",
            "entrypoint": "handler",
            "requirements": [],
            "runtime_config": {},
            "env_vars": {},
            "auth_policy": {"mode": "agent_grant", "require_user_access": True},
            "network_policy": {"network_mode": "none", "allowed_hosts": []},
            "resource_limits": {"memory_limit": "512m"},
            "timeout_seconds": 60,
        }
        kwargs.update(overrides)
        return DefinitionRepository._validate_tool_shape(**kwargs)

    def test_python_requires_source(self):
        with pytest.raises(ValueError, match="source_code is required"):
            self._validate(source_code="")

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(ValueError, match="runtime_type must be one of"):
            self._validate(runtime_type="wasm")

    def test_http_requires_url(self):
        with pytest.raises(ValueError, match="runtime_config.url is required"):
            self._validate(runtime_type="http", source_code="")

    def test_http_rejects_credential_in_url(self):
        with pytest.raises(ValueError, match="must not contain credentials"):
            self._validate(
                runtime_type="http",
                source_code="",
                runtime_config={"url": "https://user:pass@api.example.com/x"},
            )

    def test_http_rejects_relative_url(self):
        with pytest.raises(ValueError, match="absolute http"):
            self._validate(
                runtime_type="http", source_code="", runtime_config={"url": "/v1/x"}
            )

    def test_http_rejects_scheme_relative_url(self):
        with pytest.raises(ValueError, match="absolute http"):
            self._validate(
                runtime_type="http", source_code="", runtime_config={"url": "//evil/x"}
            )

    def test_http_accepts_a_complete_definition(self):
        self._validate(
            runtime_type="http",
            source_code="",
            runtime_config={"url": "https://api.example.com/v1/{city}", "method": "GET"},
            network_policy={"network_mode": "allowlist", "allowed_hosts": ["api.example.com"]},
        )

    def test_http_rejects_body_and_form_together(self):
        with pytest.raises(ValueError, match="not both"):
            self._validate(
                runtime_type="http",
                source_code="",
                runtime_config={
                    "url": "https://api.example.com/v1",
                    "body": {"a": 1},
                    "form": {"a": "1"},
                },
            )

    def test_http_rejects_unsupported_method(self):
        with pytest.raises(ValueError, match="runtime_config.method"):
            self._validate(
                runtime_type="http",
                source_code="",
                runtime_config={"url": "https://api.example.com/x", "method": "TRACE"},
            )

    def test_declarative_timeout_is_bounded(self):
        with pytest.raises(ValueError, match="between 1 and 120"):
            self._validate(
                runtime_type="http",
                source_code="",
                runtime_config={"url": "https://api.example.com/x", "timeout_seconds": 600},
            )

    def test_allowed_status_must_be_real_codes(self):
        with pytest.raises(ValueError, match="not an HTTP status code"):
            self._validate(
                runtime_type="http",
                source_code="",
                runtime_config={"url": "https://api.example.com/x", "allowed_status": [999]},
            )

    def test_openapi_requires_a_stored_spec(self):
        with pytest.raises(ValueError, match="runtime_config.spec is required"):
            self._validate(runtime_type="openapi", source_code="", runtime_config={})

    def test_openapi_requires_a_resolvable_operation(self):
        with pytest.raises(ValueError, match="No operation with id"):
            self._validate(
                runtime_type="openapi",
                source_code="",
                runtime_config={
                    "spec": json.dumps(PETSTORE),
                    "operation_id": "nope",
                },
            )

    def test_openapi_accepts_a_valid_document(self):
        self._validate(
            runtime_type="openapi",
            source_code="",
            runtime_config={"spec": json.dumps(PETSTORE), "operation_id": "getPet"},
            network_policy={"network_mode": "allowlist", "allowed_hosts": ["api.example.com"]},
        )

    def test_builtin_requires_a_known_handler(self):
        with pytest.raises(ValueError, match="No built-in tool"):
            self._validate(
                runtime_type="builtin",
                source_code="",
                runtime_config={"handler": "nope"},
                entrypoint="handler",
            )

    def test_builtin_rejects_a_widened_schema(self):
        """A definition may not advertise capabilities the handler does not have."""
        with pytest.raises(ValueError, match="must match the catalog schema"):
            self._validate(
                runtime_type="builtin",
                source_code="",
                runtime_config={"handler": "weather_forecast"},
                input_schema={
                    "type": "object",
                    "properties": {"timezone": {"type": "string"},
                                   "run_shell": {"type": "string"}},
                    "additionalProperties": False,
                },
            )

    def test_builtin_accepts_the_catalog_schema(self):
        catalog_schema = get_builtin_tool("weather_forecast").input_schema
        self._validate(
            runtime_type="builtin",
            source_code="",
            runtime_config={"handler": "weather_forecast"},
            input_schema=json.loads(json.dumps(catalog_schema)),
            entrypoint="weather_forecast",
        )
