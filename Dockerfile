# Distroless, non-root, read-only-rootfs friendly image.
# Build-stage Python MUST match the distroless runtime's Python minor version
# (distroless/python3-debian12 is 3.11) — otherwise packages land in a
# python3.x/site-packages path the runtime can't see and every import fails.
FROM python:3.11-slim AS build
WORKDIR /app
COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[azure,aws]"

FROM gcr.io/distroless/python3-debian12:nonroot
COPY --from=build /install /usr/local
COPY --from=build /app/src /app/src
# distroless python's sys.path does NOT include /usr/local/lib/.../site-packages,
# so the copied deps must be put on PYTHONPATH explicitly (alongside the app source).
ENV PYTHONPATH=/app/src:/usr/local/lib/python3.11/site-packages
USER nonroot
# Default to the remote HTTP transport for the gateway deployment.
ENTRYPOINT ["python", "-m", "k8s_sre_agent.server"]
CMD ["http", "--host", "0.0.0.0", "--port", "8080"]
