# Rootfs for ComputeSDK benchmarks (burst_TTI & DAX)
# Provides Node.js (>= 22), git, curl, unzip, tar, bash, and base build tools.

FROM node:22-bookworm-slim

# Prevent apt from dropping privileges in gVisor sandbox
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/01sandbox-user

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    git \
    procps \
    python3 \
    python3-setuptools \
    tar \
    unzip \
 && rm -rf /var/lib/apt/lists/*

# Install node-gyp globally
RUN npm install -g node-gyp \
 && ln -s /usr/local/bin/node-gyp /usr/bin/node-gyp 2>/dev/null || true

# Stub out chown/fchown so gVisor returns success when extracting archives
RUN echo 'int fchown(int fd, unsigned int owner, unsigned int group) { return 0; }' > /tmp/nochown.c && \
    echo 'int chown(const char *path, unsigned int owner, unsigned int group) { return 0; }' >> /tmp/nochown.c && \
    echo 'int fchownat(int dirfd, const char *path, unsigned int owner, unsigned int group, int flags) { return 0; }' >> /tmp/nochown.c && \
    echo 'int lchown(const char *path, unsigned int owner, unsigned int group) { return 0; }' >> /tmp/nochown.c && \
    gcc -shared -fPIC -o /usr/local/lib/libnochown.so /tmp/nochown.c && \
    rm /tmp/nochown.c && \
    echo "/usr/local/lib/libnochown.so" >> /etc/ld.so.preload

WORKDIR /workspace
