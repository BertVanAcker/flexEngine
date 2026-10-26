# Minimal flexNode stand-in, built FROM a locally present base image (no registry pull needed).
ARG BASE=alpine:3.20
FROM ${BASE}
ARG FLEX_APP=flexnode
ARG FLEX_VERSION=0.0.0
ARG FLEX_FAIL_HEALTH=0
ENV FLEX_APP=${FLEX_APP} FLEX_VERSION=${FLEX_VERSION} FLEX_FAIL_HEALTH=${FLEX_FAIL_HEALTH}
COPY run.sh health.sh /app/
RUN chmod +x /app/run.sh /app/health.sh
LABEL dev.flex.app=${FLEX_APP} dev.flex.version=${FLEX_VERSION}
EXPOSE 8080
ENTRYPOINT ["/app/run.sh"]
