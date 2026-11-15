# Strangler router + schema probe image (migrate/router/, docs/phase2-plan.md sections 3 and 9).
# Copied to <project>/harness/router/ and built by `parity harness build PROJECT --composite` with
# `docker build --network none --pull=false`: the base image must already be present locally, and no RUN step
# needs the network (there are none). Standard library only; runs read-only, non-root, on the internal network.
# TODO(M6): pin the base image by digest for CI, e.g.
#   docker buildx imagetools inspect python:3.12.7-alpine3.20  ->  FROM python:3.12.7-alpine3.20@sha256:<digest>
FROM python:3.12.7-alpine3.20
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY proxy.py schema_probe.py /app/
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "/app/proxy.py"]
