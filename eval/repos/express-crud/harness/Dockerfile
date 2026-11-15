# Harness image for the express-crud fixture (legacy service), used by `migration-agent harness build`.
# Built with `docker build --network none --pull=false`: no RUN step needs the network. Dependencies are
# installed later by the harness build job (phase "build": package-mirror-only network, or none).
# TODO(M1): pin the base image by digest for CI, e.g.
#   docker buildx imagetools inspect node:20.18.0-bookworm  ->  FROM node:20.18.0-bookworm@sha256:<digest>
# The full (non-slim) image ships python3/make/g++, so sqlite3 compiles from source against the bundled
# node headers (npm_config_nodedir) instead of downloading a prebuilt binary from a non-mirror host.
FROM node:20.18.0-bookworm
ENV HOME=/tmp \
    NODE_ENV=production \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    npm_config_nodedir=/usr/local \
    npm_config_build_from_source=true
WORKDIR /work/app
USER 10001:10001
