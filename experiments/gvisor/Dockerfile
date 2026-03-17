FROM nikolaik/python-nodejs:python3.11-nodejs20

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONPATH=/opt/hermes-agent
ENV HERMES_HOME=/var/lib/hermes
ENV TERMINAL_ENV=docker
ENV TERMINAL_DOCKER_IMAGE=nikolaik/python-nodejs:python3.11-nodejs20

WORKDIR /opt/hermes-agent

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/hermes-agent

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -e . \
    && python -m pip install -e ./mini-swe-agent

RUN mkdir -p /var/lib/hermes /workspace

COPY experiments/gvisor/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["smoke"]
