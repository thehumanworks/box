# The sandbox image: Ubuntu with the "sandbox" home of this repository's Nix flake.
#
# The flake defines every tool. This file only puts the result into an image that both
# Modal and Vercel Sandbox accept:
#   - linux/amd64 only. Both providers require it.
#   - No ENTRYPOINT and no CMD. Vercel Sandbox ignores them, so nothing may depend on them.
#   - No USER. Modal always runs as root, so the home is root's.
#   - Ubuntu as the base, so that programs which expect a usual Linux system also run,
#     for example the tools that mise downloads.
#   - No secrets. The image is public.
#
# The build context is the repository root. .dockerignore limits it to the flake.

# Stage 1: build the home with Nix and copy it, with all it needs, to a new Nix store.
FROM nixos/nix:2.35.6@sha256:b49cb33f3a7e5cf3933ad8774529de0e3807ec5cf54987aa7ccdfb49f2fc55ed AS home

ENV NIX_CONFIG="experimental-features = nix-command flakes"

WORKDIR /src
COPY flake.nix flake.lock ./
COPY nix ./nix
COPY home ./home

# The symbolic link in gcroots gives the home a fixed address in the image. It also keeps
# the Nix garbage collector from deleting the home.
RUN home="$(nix build --no-link --print-out-paths path:.#sandbox-home)" \
    && nix copy --no-check-sigs --to "local?root=/out" "$home" \
    && mkdir -p /out/nix/var/nix/gcroots \
    && ln -s "$home" /out/nix/var/nix/gcroots/sandbox-home

# Stage 2: the image.
FROM ubuntu:noble-20261110@sha256:c1c5d8818eca0a4fd0c6c187d459a7af264e7fbf8561bde85e93be2a3bbc8f4a

# Programs from Nix read the certificates of the system.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=home /out/nix /nix

# Home Manager needs USER. The profile directory holds every tool of the home.
ENV USER=root
ENV PATH="/root/.nix-profile/bin:${PATH}"

# Activate the home: this links the tools into the profile and the files into /root.
# Ubuntu's own shell files go first, because the home brings its own. The Nix settings of
# the home are not in place before the activation, so NIX_CONFIG passes them for this step.
RUN rm -f /root/.bashrc /root/.profile \
    && home=/nix/var/nix/gcroots/sandbox-home \
    && PATH="${home}/home-path/bin:${PATH}" \
       NIX_CONFIG="$(cat "${home}/home-files/.config/nix/nix.conf")" \
       "${home}/activate"

WORKDIR /workspace
