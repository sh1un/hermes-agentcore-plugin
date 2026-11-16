# Build the plugin runtime from immutable upstream and plugin source inputs.
ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent:v0.23.0@sha256:adb7890916de63245523492be8f68f9d2095fecbec4e2a6dab7960381f0fc002
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85
FROM ${UV_IMAGE} AS uv
FROM ${HERMES_BASE_IMAGE}
USER root
COPY --from=uv /uv /usr/local/bin/uv
ARG PLUGIN_REVISION
LABEL org.opencontainers.image.title="Hermes AgentCore Plugin runtime"
LABEL io.hermes.agentcore.plugin.revision=${PLUGIN_REVISION}
COPY plugin/ /opt/hermes/plugins/agentcore/
# Use the plugin's reviewed pins rather than inheriting the host project's
# rolling exclude-newer quarantine, which rejects this plugin's AWS SDK.
# Hermes v0.23.0 browser-harness requires this Pillow version.
RUN uv --no-config pip install --python /opt/hermes/.venv/bin/python --no-cache-dir -r /opt/hermes/plugins/agentcore/requirements.txt 'pillow==12.3.0' \
    && uv pip check --python /opt/hermes/.venv/bin/python
COPY check-host.py /tmp/check-host.py
RUN /opt/hermes/.venv/bin/python /tmp/check-host.py
# Explicitly bypass the base image's s6/local-patch/bootstrap paths.
ENV HOME=/opt/data HERMES_HOME=/opt/data PYTHONPATH=/opt/hermes
USER 10000:10000
ENTRYPOINT ["/opt/hermes/.venv/bin/hermes"]
CMD ["gateway", "run"]
