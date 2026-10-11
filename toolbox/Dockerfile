# Example MCP servers for tenantfabric: tools.py (shared tools) and runner.py (per-team CLI runner).
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/anindyar/tenantfabric" \
      org.opencontainers.image.description="tenantfabric example MCP servers: shared tools and a per-team CLI runner (curl, aws, jq)" \
      org.opencontainers.image.licenses="Apache-2.0"
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl jq unzip \
 && case "$TARGETARCH" in arm64) A=aarch64 ;; *) A=x86_64 ;; esac \
 && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$A.zip" -o /tmp/awscli.zip \
 && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip \
 && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "mcp[cli]==1.*" httpx \
 && useradd -u 10001 -m runner
RUN mkdir -p /etc/runner/aws && touch /etc/runner/aws/config /etc/runner/aws/credentials   # root-owned, read-only to the runner
COPY tools.py runner.py /app/
USER 10001
CMD ["python", "/app/tools.py"]
