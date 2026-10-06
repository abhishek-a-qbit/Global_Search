# Dev-only workaround for Docker Desktop on WSL kernels without /proc/self/io
# (e.g. WSL 3.x / kernel 6.18): typesense-server segfaults at startup when that file
# is missing. This preloads a tiny shim that serves a static copy instead.
# Not needed on a normal Linux host; production uses the plain image.
ARG TYPESENSE_VERSION=30.2

FROM ubuntu:22.04 AS shim
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY shim.c /shim.c
RUN gcc -shared -fPIC -O2 -o /libprocio.so /shim.c -ldl

FROM typesense/typesense:${TYPESENSE_VERSION}
COPY --from=shim /libprocio.so /usr/local/lib/libprocio.so
COPY fake-proc-io /etc/fake-proc-io
ENV LD_PRELOAD=/usr/local/lib/libprocio.so
