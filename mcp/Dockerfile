# Stdio MCP server in a container: the image Docker's MCP registry
# (hub.docker.com/mcp, Docker Desktop's MCP Toolkit) builds from this
# directory. It is the containerised equivalent of `uvx apiguru-mcp`.
#
# The agent-kit root Dockerfile is a different thing: it builds the hosted
# gateway + streamable-HTTP MCP pair that runs on agent./mcp.apiguru.app.
#
#   docker build -t apiguru-mcp ./mcp
#   docker run -i --rm apiguru-mcp                          # keyless
#   docker run -i --rm -e APIGURU_API_KEY=... apiguru-mcp   # billed to an account
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src /root/.cache

# The MCP Toolkit runs servers as an unprivileged user anyway; do it here too
# so the image is safe under any runtime.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin apiguru
USER apiguru
WORKDIR /home/apiguru

ENTRYPOINT ["apiguru-mcp"]
