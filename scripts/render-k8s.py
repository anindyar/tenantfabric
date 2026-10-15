#!/usr/bin/env python3
"""Render k8s/tenantfabric.yaml with your own keys and passwords instead of the demo values.

    python3 scripts/render-k8s.py .env > tenantfabric.local.yaml     # *.local.yaml is git-ignored
    kubectl apply -f tenantfabric.local.yaml

Reads the same .env as Docker Compose (see .env.example). Every variable must be set; the script refuses
to emit a manifest that still contains a demo value. No dependencies beyond Python 3."""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# demo literal in k8s/tenantfabric.yaml  ->  variable that replaces it
REPLACE = [
    ("sk-alpha-demo", "TF_KEY_ALPHA"),
    ("sk-beta-demo", "TF_KEY_BETA"),
    ("sk-memsvc-demo", "TF_KEY_MEMSVC"),
    ("POSTGRES_PASSWORD: tenantfabric", "POSTGRES_PASSWORD: {TF_POSTGRES_PASSWORD}"),
    ("postgres:tenantfabric@", "postgres:{TF_POSTGRES_PASSWORD}@"),
    ("memsvc:memsvc@", "memsvc:{TF_MEMSVC_DB_PASSWORD}@"),
    ("login password 'memsvc'", "login password '{TF_MEMSVC_DB_PASSWORD}'"),
    ("AKIAALPHADEMOEXAMPLE", "ALPHA_AWS_ACCESS_KEY_ID"),
    ("alpha-demo-secret-not-real", "ALPHA_AWS_SECRET_ACCESS_KEY"),
]


def load_env(path):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    env = load_env(sys.argv[1])
    text = open(os.path.join(ROOT, "k8s", "tenantfabric.yaml")).read()
    for demo, target in REPLACE:
        if "{" in target:
            names = [n.split("}")[0] for n in target.split("{")[1:]]
        else:
            names, target = [target], "{" + target + "}"
        missing = [n for n in names if not env.get(n)]
        if missing:
            sys.exit(f"missing in {sys.argv[1]}: {', '.join(missing)}")
        for n in names:
            if any(c in env[n] for c in "'\"\\\n@:/ "):
                sys.exit(f"{n}: use letters, digits, - and _ only")
        if demo not in text:
            sys.exit(f"demo value {demo!r} not found: k8s/tenantfabric.yaml changed, update scripts/render-k8s.py")
        text = text.replace(demo, target.format(**env))
    leftovers = [d for d, _ in REPLACE if d in text]
    if leftovers:
        sys.exit(f"demo values left: {leftovers}")
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
