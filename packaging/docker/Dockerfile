FROM python:3.11.16-alpine3.23@sha256:7671276afa87f02dd3ab1b61544d4bc41914e067d065676a4306c278b4fa59b4
ARG VCS_REF=unknown
LABEL org.algosec.jira-bus.image="0.2.4" \
      org.opencontainers.image.title="Jira-FireFlow Bus" \
      org.opencontainers.image.description="Outbound-only Jira Cloud to AlgoSec FireFlow integration" \
      org.opencontainers.image.source="https://github.com/kdimiter/jira-fireflow-bus/" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.version="0.2.4" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/algosec-jira-bus/setup-source
COPY pyproject.toml ./
COPY algosec_jira_bus ./algosec_jira_bus
COPY scripts/setup_wizard.py ./scripts/setup_wizard.py
COPY examples/jira-sync-basic-structured.json ./examples/jira-sync-basic-structured.json
COPY LICENSE /licenses/Apache-2.0.txt
COPY packaging/docker/scheduler.py /usr/local/libexec/algosec-jira-bus-scheduler.py
RUN apk add --upgrade --no-cache 'libuuid=2.41.6-r1' \
 && python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
 && rm -rf pyproject.toml algosec_jira_bus algosec_jira_bus.egg-info \
 && rm -rf /usr/local/lib/python3.11/site-packages/pip \
            /usr/local/lib/python3.11/site-packages/pip-*.dist-info \
            /usr/local/lib/python3.11/site-packages/setuptools \
            /usr/local/lib/python3.11/site-packages/setuptools-*.dist-info \
            /usr/local/lib/python3.11/site-packages/wheel \
            /usr/local/lib/python3.11/site-packages/wheel-*.dist-info \
            /usr/local/lib/python3.11/site-packages/packaging \
            /usr/local/lib/python3.11/site-packages/packaging-*.dist-info \
 && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.11 /usr/local/bin/wheel \
 && addgroup -g 10001 -S algosec-jira-bus \
 && adduser -u 10001 -S -D -H -s /sbin/nologin -G algosec-jira-bus algosec-jira-bus \
 && mkdir -p /etc/algosec-jira-bus /var/lib/algosec-jira-bus \
 && chown 10001:10001 /var/lib/algosec-jira-bus \
 && chmod 0700 /var/lib/algosec-jira-bus \
 && chmod -R a+rX /opt/algosec-jira-bus/setup-source /usr/local/libexec/algosec-jira-bus-scheduler.py
USER 10001:10001
ENTRYPOINT ["python", "/usr/local/libexec/algosec-jira-bus-scheduler.py"]
CMD ["serve"]
