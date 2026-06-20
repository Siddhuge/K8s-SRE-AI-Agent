# Distroless, non-root, read-only-rootfs friendly image.
# Build-stage Python MUST match the distroless runtime's Python minor version
# (distroless/python3-debian12 is 3.11) — otherwise packages land in a
# python3.x/site-packages path the runtime can't see and every import fails.
FROM python:3.11-slim AS build
WORKDIR /app
COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
COPY webapp ./webapp
# [web] pulls in anthropic for the v2 dashboard chat; [azure,aws] for cloud cluster auth.
RUN pip install --no-cache-dir --prefix=/install ".[azure,aws,web]"

FROM gcr.io/distroless/python3-debian12:nonroot
COPY --from=build /install /usr/local
COPY --from=build /app/src /app/src
COPY --from=build /app/webapp /app/webapp
COPY --from=build /app/README.md /app/README.md
# distroless python's sys.path does NOT include /usr/local/lib/.../site-packages, so the
# copied deps must be on PYTHONPATH explicitly. /app lets `import webapp` resolve.
ENV PYTHONPATH=/app:/app/src:/usr/local/lib/python3.11/site-packages
USER nonroot
EXPOSE 8080 8081
# Default: the MCP gateway (v1). Run the v2 dashboard instead with:
#   docker run --entrypoint python <image> -m webapp.server   (serves :8081)
ENTRYPOINT ["python", "-m", "k8s_sre_agent.server"]
CMD ["http", "--host", "0.0.0.0", "--port", "8080"]
