FROM node:22.23.2-bookworm-slim AS hub-build
WORKDIR /hub
COPY server/src/agentmesh_server/vendor/openhuddle/ ./
# The vendored hub has no npm dependencies or transpilation step.
RUN node --check agentmesh-boot.js \
    && node --input-type=module -e "import { openDb } from './src/hub/db.js'; openDb().close()"

FROM python:3.11.14-slim-bookworm AS python-build
WORKDIR /build
COPY server/pyproject.toml ./
COPY server/src/ ./src/
COPY --from=hub-build /hub/ ./src/agentmesh_server/vendor/openhuddle/
RUN python -m pip wheel --no-cache-dir --no-build-isolation --no-index --no-deps --wheel-dir /wheels .

FROM python:3.11.14-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTMESH_SERVER_DIR=/data \
    HOME=/data
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 libatomic1 sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 agentmesh \
    && useradd --uid 10001 --gid agentmesh --no-create-home --home-dir /data agentmesh \
    && install -d -o agentmesh -g agentmesh -m 0700 /data
COPY --from=hub-build /usr/local/bin/node /usr/local/bin/node
COPY --from=python-build /wheels/ /wheels/
RUN python -m pip install --no-cache-dir --no-index /wheels/*.whl \
    && rm -rf /wheels
COPY scripts/sqlite-backup.sh /usr/local/bin/agentmesh-backup
RUN chmod 0555 /usr/local/bin/agentmesh-backup
USER 10001:10001
WORKDIR /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).close()"]
ENTRYPOINT ["agentmesh-server"]
CMD ["serve", "--host", "0.0.0.0", "--allow-remote-bind"]
