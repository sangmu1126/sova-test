#!/bin/sh
# build-image.sh <image:tag> [--kind <cluster>]
#
# Builds the service image. SOVA_SECURITY_SRC names a checkout of the pinned
# sova-security release (its wheel goes into the image); in CI the wheel
# comes from the private package index instead. With --kind the image is
# loaded into that local kind cluster (nothing is pushed anywhere).
set -eu
image=${1:?image:tag required}
shift
kind_cluster=
if [ "${1:-}" = --kind ]; then kind_cluster=${2:?kind cluster name required}; fi
root=$(cd "$(dirname "$0")/../.." && pwd)
src=${SOVA_SECURITY_SRC:?SOVA_SECURITY_SRC must name the pinned sova-security checkout}
wheels=$(mktemp -d)
trap 'rm -rf "$wheels"' EXIT
python3 -m pip wheel --quiet --no-deps --wheel-dir "$wheels" "$src"
ls "$wheels"/sova_security-0.11.1-*.whl >/dev/null  # the version pyproject pins
docker build --file "$root/deploy/service/Dockerfile" --build-context "wheels=$wheels" \
  --tag "$image" "$root"
if [ -n "$kind_cluster" ]; then
  kind load docker-image "$image" --name "$kind_cluster"
fi
