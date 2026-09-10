FROM python:3.11-slim-bookworm
LABEL org.algosec.jira-bus.image="0.2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/algosec-jira-bus/setup-source
COPY . .
RUN python -m pip install --no-cache-dir vendor/*.whl . \
 && groupadd --gid 10001 algosec-jira-bus \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin algosec-jira-bus \
 && mkdir -p /etc/algosec-jira-bus /var/lib/algosec-jira-bus \
 && chown 10001:10001 /var/lib/algosec-jira-bus \
 && chmod 0700 /var/lib/algosec-jira-bus \
 && chmod -R a+rX /opt/algosec-jira-bus/setup-source
USER 10001:10001
ENTRYPOINT ["python", "/opt/algosec-jira-bus/setup-source/packaging/docker/scheduler.py"]
CMD ["serve"]
