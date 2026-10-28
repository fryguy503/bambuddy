#!/bin/sh
# Compatibility build for Synology Docker 24 runtimes without seccomp support.
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

# Keep one authoritative Dockerfile. Only the two dependency-cache mounts need
# removing for Docker's classic builder; dependencies and runtime stay the same.
dockerfile=$(mktemp "$repo_dir/.Dockerfile.synology.XXXXXX")
trap 'rm -f -- "$dockerfile"' EXIT
trap 'exit 1' HUP INT TERM

awk '
    { sub(/\r$/, "") }
    /^RUN --mount=type=cache,target=\/root\/\.(npm|cache\/pip) \\$/ {
        print "RUN \\"
        next
    }
    { print }
' Dockerfile > "$dockerfile"

# Do not silently reinterpret future secret/bind mounts or other BuildKit flags.
if grep -Eq '^RUN --' "$dockerfile"; then
    echo "Unsupported RUN option in Dockerfile; update the Synology compatibility build." >&2
    exit 1
fi

DOCKER_BUILDKIT=0 docker build --force-rm --file "$dockerfile" \
    --tag bambuddy:anycubic-https-testing .
