# Base image for agent sandboxes. The same image runs on Modal and on Vercel Sandbox.
#
# Rules that keep the image portable between the two providers:
#   - linux/amd64 only. Both providers require it.
#   - No ENTRYPOINT and no CMD. Vercel Sandbox ignores them, so nothing may depend on them.
#   - No USER. Modal always runs as root, so the image is built for root.
#   - Nothing may depend on ENV alone. Every tool is also linked into /usr/local/bin,
#     which is on the default PATH of every Linux process.
#   - Only FROM, ARG, ENV, RUN, COPY and WORKDIR, without BuildKit-only syntax. Modal has
#     its own Dockerfile builder, and this subset lets it build the file for local tests.
#   - No secrets. The image is public.
#
# The build context is this directory (image/).

FROM ubuntu:noble-20261110@sha256:c1c5d8818eca0a4fd0c6c187d459a7af264e7fbf8561bde85e93be2a3bbc8f4a

ARG MISE_VERSION=2026.10.17
ARG MISE_SHA256=fe6dd17e68e09682b31d2e7bb7bb755d22636ac1af5d94621e300f2ca9e9fe8a

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        openssh-client \
        ripgrep \
        unzip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# mise is the tool manager. It is one static file, checked against a pinned checksum.
RUN curl -fsSL -o /usr/local/bin/mise \
        "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-x64" \
    && echo "${MISE_SHA256}  /usr/local/bin/mise" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/mise

# The system-wide mise config and its lockfile define the agent tools and their versions.
COPY mise.toml /etc/mise/config.toml
COPY mise.lock /etc/mise/mise.lock

# Install the tools for all users (/usr/local/share/mise) from the lockfile only.
# Then link each shim into /usr/local/bin, so the tools work even if a provider drops ENV.
RUN chmod 0644 /etc/mise/config.toml /etc/mise/mise.lock \
    && mise install --system --locked \
    && mise reshim --system \
    && ln -s /usr/local/share/mise/shims/* /usr/local/bin/ \
    && rm -rf /root/.cache/mise \
    && echo 'eval "$(mise activate bash)"' >> /etc/bash.bashrc

# Shims first: the user's own tools (added later with `mise use -g`), then the system tools.
ENV PATH="/root/.local/share/mise/shims:/usr/local/share/mise/shims:${PATH}"

WORKDIR /workspace
