# Build in CI from immutable source inputs, never pip-install on the EC2 host.
ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent@sha256:64923faeae267792bf9bf87fe3b4c4869e35004e360c7df01730ad801b74d524
FROM ${HERMES_BASE_IMAGE}
USER root
ARG PLUGIN_REVISION
LABEL org.opencontainers.image.title="Hermes AgentCore Plugin staging runtime"
LABEL io.hermes.agentcore.plugin.revision=${PLUGIN_REVISION}
COPY plugin/ /opt/hermes/plugins/agentcore/
# Use the plugin's reviewed pins rather than inheriting the host project's
# rolling exclude-newer quarantine, which rejects this plugin's AWS SDK.
RUN uv --no-config pip install --python /opt/hermes/.venv/bin/python --no-cache-dir -r /opt/hermes/plugins/agentcore/requirements.txt 'msal==1.38.0' \
    && uv pip check --python /opt/hermes/.venv/bin/python
COPY check-host.py /tmp/check-host.py
RUN /opt/hermes/.venv/bin/python /tmp/check-host.py
# Explicitly bypass the base image's s6/local-patch/bootstrap paths.
ENV HOME=/opt/data HERMES_HOME=/opt/data PYTHONPATH=/opt/hermes
USER 10000:10000
ENTRYPOINT ["/opt/hermes/.venv/bin/hermes"]
CMD ["gateway", "run"]
