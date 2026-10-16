"""Check that the copies tenantfabric keeps in two places agree (no services needed):

    python3 tests/check_sync.py

- the OPA policy:   policy/policy.rego  ==  k8s ConfigMap opa-policy (policy.rego)
- the API spec:     docs/openapi.yaml   ==  k8s ConfigMap docs-openapi (openapi.yaml)
- the gateway config and the DB schema: docker-compose.yml (with its demo defaults) == k8s Secret / ConfigMap
Exit code 0 = in sync. Needs PyYAML."""
import os, re, sys
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
read = lambda p: open(os.path.join(ROOT, p)).read()
defaults = lambda s: re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", s)   # what compose renders with no .env

k8s = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(read("k8s/tenantfabric.yaml")) if d}
compose = yaml.safe_load(read("docker-compose.yml"))
pairs = [
    ("OPA policy", read("policy/policy.rego"), k8s[("ConfigMap", "opa-policy")]["data"]["policy.rego"]),
    ("API spec", read("docs/openapi.yaml"), k8s[("ConfigMap", "docs-openapi")]["data"]["openapi.yaml"]),
    ("OPA config", compose["configs"]["opa-config"]["content"], k8s[("ConfigMap", "opa-policy")]["data"]["config.yaml"]),
    ("gateway config", defaults(compose["configs"]["agentgateway"]["content"]),
     k8s[("Secret", "agentgateway-config")]["stringData"]["config.yaml"]),
    ("DB schema", defaults(compose["configs"]["schema"]["content"]), k8s[("ConfigMap", "postgres-init")]["data"]["01-schema.sql"]),
]
bad = 0
for name, a, b in pairs:
    same = a.strip() == b.strip()
    bad += not same
    print(("ok    " if same else "DIFF  ") + name)
    if not same:
        import difflib
        sys.stdout.writelines(list(difflib.unified_diff(a.strip().splitlines(True), b.strip().splitlines(True),
                                                        "compose/source", "k8s", n=1))[:40])
sys.exit(1 if bad else 0)
