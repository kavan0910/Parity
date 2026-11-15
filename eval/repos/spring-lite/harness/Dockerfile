# Harness image for the spring-lite fixture (legacy service), used by `parity harness build`.
# Built with `docker build --network none --pull=false`: no RUN step needs the network. Maven resolves
# dependencies in the harness build job (phase "build": package-mirror-only network, or offline from /cache).
# TODO(M1): pin the base image by digest for CI, e.g.
#   docker buildx imagetools inspect maven:3.9.9-eclipse-temurin-17  ->  FROM maven:3.9.9-eclipse-temurin-17@sha256:<digest>
# One image builds and runs the service (JDK 17: the fixture uses jakarta.persistence, i.e. Spring Boot 3).
FROM maven:3.9.9-eclipse-temurin-17
ENV HOME=/tmp \
    MAVEN_CONFIG=/tmp/.m2 \
    JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75 -Djava.io.tmpdir=/tmp"
WORKDIR /work/app
USER 10001:10001
