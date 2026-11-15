# Harness image for the django-lite fixture (legacy service), used by `migration-agent harness build`.
# Built with `docker build --network none --pull=false`: no RUN step needs the network. Django is installed
# by the harness build job into /work/app/.deps (phase "build": package-mirror-only network, or none).
# TODO(M1): pin the base image by digest for CI, e.g.
#   docker buildx imagetools inspect python:3.10.15-slim-bookworm  ->  FROM python:3.10.15-slim-bookworm@sha256:<digest>
# Python 3.10: the newest interpreter Django 3.2 (the legacy version) supports.
FROM python:3.10.15-slim-bookworm
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    PYTHONPATH=/work/app/.deps:/work/app \
    DJANGO_SETTINGS_MODULE=harness_settings
WORKDIR /work/app
USER 10001:10001
