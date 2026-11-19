FROM python:3.13.18-slim-trixie AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt


FROM python:3.13.18-slim-trixie AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The audit directory is created here so a fresh named volume inherits the non-root ownership.
RUN groupadd --system --gid 10001 webhook \
    && useradd --system --uid 10001 --gid webhook --no-create-home --shell /usr/sbin/nologin webhook \
    && mkdir -p /var/log/remediation \
    && chown 10001:10001 /var/log/remediation

WORKDIR /srv
COPY --from=builder /opt/venv /opt/venv
COPY main.py circuit_breaker.py forensics.py ./

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
