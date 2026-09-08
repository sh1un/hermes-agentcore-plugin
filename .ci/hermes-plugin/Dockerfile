# Build in CI from immutable source inputs, never pip-install on the EC2 host.
ARG HERMES_BASE_IMAGE
FROM ${HERMES_BASE_IMAGE}
USER root
ARG PLUGIN_REVISION
LABEL org.opencontainers.image.title="Hermes AgentCore Plugin staging runtime"
LABEL io.hermes.agentcore.plugin.revision=${PLUGIN_REVISION}
COPY plugin/ /opt/hermes/plugins/agentcore/
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache-dir -r /opt/hermes/plugins/agentcore/requirements.txt \
    && uv pip check --python /opt/hermes/.venv/bin/python
COPY check-host.py /tmp/check-host.py
RUN /opt/hermes/.venv/bin/python /tmp/check-host.py
# Explicitly bypass the base image's s6/local-patch/bootstrap paths.
ENV HOME=/opt/data HERMES_HOME=/opt/data PYTHONPATH=/opt/hermes
USER 10000:10000
ENTRYPOINT ["/opt/hermes/.venv/bin/hermes"]
CMD ["gateway", "run"]
