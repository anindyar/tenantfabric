# tenantfabric request policy, evaluated by OPA (envoy ext_authz plugin) on every /v1, /mcp and /cli call.
#
# The gateway's own rules decide WHO may call WHICH model or tool. They cannot see tool arguments
# (agentgateway 1.5: mcp.tool.arguments is empty during authorization), so this policy checks WHAT a
# call asks for: arguments, and headers set by the calling application.
#
# Source of truth: this file. docker-compose.yml mounts it; k8s/tenantfabric.yaml carries an inline copy
# (tests/check_sync.py fails if they differ). Unit tests: policy/policy_test.rego (`opa test policy/`).
package tenantfabric.authz

import rego.v1

http := input.attributes.request.http

headers := object.get(http, "headers", {})

body := object.get(input, "parsed_body", {})

# ---- data classification: optional header set by the calling application --------------------
valid_classes := {"public", "internal", "confidential", "restricted"}

data_class := lower(object.get(headers, "x-data-class", "confidential"))

reasons contains msg if {
	not data_class in valid_classes
	msg := sprintf("unknown x-data-class %q (use public, internal, confidential or restricted)", [data_class])
}

# ---- MCP tool arguments -----------------------------------------------------------------------
tool_call if body.method == "tools/call"

tool := body.params.name if tool_call

args := object.get(body.params, "arguments", {}) if tool_call

# Never store credentials in memory.
secret_re := `(?i)\b(sk|pk|rk)-[a-z0-9_\-]{16,}|\bAKIA[0-9A-Z]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bghp_[A-Za-z0-9]{30,}`

reasons contains "memory_write: refusing to store a credential or private key in memory" if {
	tool == "memory_write"
	regex.match(secret_re, object.get(args, "text", ""))
}

# http_get fetches from inside the platform: https only, public hosts only (no SSRF into the cluster).
internal_url_re := `(?i)^https?://([0-9.]+|\[[0-9a-f:]+\]|localhost|[^/:]*\.(local|svc|internal|lan)|[^/:.]+)([:/?#]|$)`

reasons contains "http_get: only https URLs are allowed" if {
	tool == "http_get"
	not startswith(lower(object.get(args, "url", "")), "https://")
}

reasons contains "http_get: internal, single-label or IP-literal hosts are not allowed" if {
	tool == "http_get"
	regex.match(internal_url_re, object.get(args, "url", ""))
}

# ---- actions that need a human's approval -----------------------------------------------------
# The application sends `x-approved: true` only after a person approved the action.
approval_tools := {"memory_reindex"}

reasons contains sprintf("%s requires approval (header x-approved: true)", [tool]) if {
	tool in approval_tools
	not approved
}

destructive_re := `(?i)\b(rm|delete|terminate|destroy|drop|truncate|put-bucket-policy)\b`

reasons contains "run_command: destructive commands require approval (header x-approved: true)" if {
	tool == "run_command"
	regex.match(destructive_re, object.get(args, "command", ""))
	not approved
}

approved if lower(object.get(headers, "x-approved", "")) == "true"

# ---- decision -----------------------------------------------------------------------------------
allowed if count(reasons) == 0

result := {"allowed": true, "headers": {"x-data-class": data_class}} if allowed

result := {
	"allowed": false,
	"http_status": 403,
	"headers": {"x-policy-decision": "deny", "content-type": "application/json"},
	"body": json.marshal({"error": {"type": "policy_denied", "message": concat("; ", sort(reasons))}}),
} if {
	not allowed
}
