# Unit tests for policy.rego:  opa test policy/ -v
package tenantfabric.authz_test

import rego.v1

import data.tenantfabric.authz

req(hdrs, body) := {"attributes": {"request": {"http": {"path": "/mcp", "headers": hdrs}}}, "parsed_body": body}

call(name, arguments) := {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

allowed(hdrs, body) if authz.result.allowed with input as req(hdrs, body)

denied_with(hdrs, body, fragment) if {
	r := authz.result with input as req(hdrs, body)
	not r.allowed
	r.http_status == 403
	contains(r.body, fragment)
}

# ---- plain traffic passes ----
test_llm_call_allowed if allowed({}, {})

test_tools_list_allowed if allowed({}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

test_default_class_header if {
	r := authz.result with input as req({}, {})
	r.headers["x-data-class"] == "confidential"
}

# ---- data class ----
test_known_class_allowed if allowed({"x-data-class": "Internal"}, {})

test_unknown_class_denied if denied_with({"x-data-class": "secret"}, {}, "unknown x-data-class")

# ---- memory_write ----
test_memory_write_plain_allowed if allowed({}, call("memory_write", {"text": "Deploys freeze on Fridays"}))

test_memory_write_openai_key_denied if denied_with({}, call("memory_write", {"text": "key is sk-abcdefghijklmnopqrstuv"}), "credential")

test_memory_write_aws_key_denied if denied_with({}, call("memory_write", {"text": "AKIAABCDEFGHIJKLMNOP"}), "credential")

test_memory_write_private_key_denied if denied_with({}, call("memory_write", {"text": "-----BEGIN RSA PRIVATE KEY-----"}), "private key")

# ---- http_get ----
test_http_get_public_https_allowed if allowed({}, call("http_get", {"url": "https://example.com/a?b=c"}))

test_http_get_plain_http_denied if denied_with({}, call("http_get", {"url": "http://example.com"}), "only https")

test_http_get_ip_denied if denied_with({}, call("http_get", {"url": "https://169.254.169.254/latest"}), "IP-literal")

test_http_get_cluster_name_denied if denied_with({}, call("http_get", {"url": "https://memsvc.tenantfabric.svc:8041/mcp"}), "internal")

test_http_get_single_label_denied if denied_with({}, call("http_get", {"url": "https://memsvc:8041/mcp"}), "single-label")

test_http_get_localhost_denied if denied_with({}, call("http_get", {"url": "https://localhost/"}), "internal")

# ---- approvals ----
test_reindex_without_approval_denied if denied_with({}, call("memory_reindex", {}), "requires approval")

test_reindex_with_approval_allowed if allowed({"x-approved": "true"}, call("memory_reindex", {}))

test_destructive_command_denied if denied_with({}, call("run_command", {"command": "aws s3 rm s3://bucket/key"}), "destructive")

test_destructive_command_approved if allowed({"x-approved": "TRUE"}, call("run_command", {"command": "aws s3 rm s3://bucket/key"}))

test_read_command_allowed if allowed({}, call("run_command", {"command": "aws s3 ls"}))

# ---- several reasons are all reported ----
test_reasons_combined if {
	r := authz.result with input as req({"x-data-class": "nope"}, call("memory_reindex", {}))
	contains(r.body, "requires approval")
	contains(r.body, "unknown x-data-class")
}
