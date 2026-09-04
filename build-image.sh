#!/usr/bin/env bash
#
# Build the custom Frappe image that contains Helpdesk.
#
# Helpdesk ships in no official image, so there is nothing to pull — the image
# has to be built with the app baked in. This uses frappe_docker's own
# "layered" Containerfile rather than a hand-written Dockerfile, because that
# build is maintained upstream and tracks changes to the bench/asset pipeline
# that a copy here would silently miss.
#
# The Containerfile layers on top of frappe/build:<branch> and
# frappe/base:<branch> — official pre-built images already carrying the
# Python/Node toolchain for that Frappe branch — rather than compiling
# Frappe's own toolchain from source. apps.json is passed in as a BuildKit
# *secret*, not a build-arg, so it never lands in an image layer or the
# build cache.
#
# Usage:
#   ./build-image.sh                    # builds sis/frappe-helpdesk:v15
#   IMAGE=myrepo/helpdesk TAG=v15 ./build-image.sh
#
# Requires: docker with BuildKit (docker buildx), git.

set -euo pipefail

IMAGE="${IMAGE:-sis/frappe-helpdesk}"
TAG="${TAG:-v15}"

# Pin all three. An unpinned build is how an API method quietly gets renamed
# underneath a working integration.
#   FRAPPE_DOCKER_REF — commit to check out from frappe_docker. Pinned to a
#     commit rather than a tag: this repo's tags stop at v3.2.2 and do not
#     track Frappe versions, so a commit SHA is the only stable pin available.
#   FRAPPE_BRANCH      — which frappe/build and frappe/base images to layer on.
#     Confirmed present on Docker Hub for "version-15" at the time this was
#     written; re-check if this build ever starts failing to pull.
FRAPPE_DOCKER_REF="${FRAPPE_DOCKER_REF:-380b9d069ab949754fe78331af647b673984dc04}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-15}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${HERE}/.build"

echo "==> Fetching frappe_docker @ ${FRAPPE_DOCKER_REF}"
rm -rf "${WORKDIR}"
mkdir -p "${WORKDIR}"
git -C "${WORKDIR}" init -q
# Force LF regardless of this machine's global core.autocrlf. On a Windows git
# install with autocrlf=true (the common default), checkout silently rewrites
# every LF in the repo's shell scripts to CRLF. That corrupts the shebang line
# of resources/core/entrypoint.sh — "#!/bin/bash\r" doesn't resolve to a real
# interpreter — and every long-running container in the stack crash-loops with
# "exec ...: no such file or directory" even though the file is right there.
# Scoped to this throwaway clone only; does not touch the user's git config.
git -C "${WORKDIR}" config core.autocrlf false
git -C "${WORKDIR}" remote add origin https://github.com/frappe/frappe_docker
git -C "${WORKDIR}" fetch --depth 1 origin "${FRAPPE_DOCKER_REF}"
git -C "${WORKDIR}" checkout -q FETCH_HEAD

echo "==> Building ${IMAGE}:${TAG}  (frappe branch: ${FRAPPE_BRANCH})"
echo "    apps.json: $(tr -d '\n' < "${HERE}/apps.json")"

# BuildKit deliberately excludes a --secret's *contents* from the layer cache
# key (so cache fingerprints can't leak secret data) — only the RUN command
# text is hashed. That means an apps.json edit alone is invisible to the
# cache: the `bench init` layer replays with the OLD app list, silently. The
# Containerfile's own CACHE_BUST arg exists for exactly this; hashing
# apps.json busts the cache whenever — and only when — the app list changes.
CACHE_BUST="$(sha256sum "${HERE}/apps.json" | cut -d' ' -f1)"

DOCKER_BUILDKIT=1 docker build \
  --file "${WORKDIR}/images/layered/Containerfile" \
  --build-arg=FRAPPE_PATH=https://github.com/frappe/frappe \
  --build-arg=FRAPPE_BRANCH="${FRAPPE_BRANCH}" \
  --build-arg=CACHE_BUST="${CACHE_BUST}" \
  --secret id=apps_json,src="${HERE}/apps.json" \
  --tag "${IMAGE}:${TAG}" \
  "${WORKDIR}"

rm -rf "${WORKDIR}"

echo
echo "==> Built ${IMAGE}:${TAG}"
echo "    Set HELPDESK_IMAGE=${IMAGE} and HELPDESK_TAG=${TAG} in .env, then:"
echo "    docker compose -f docker-compose.helpdesk.yml up -d"
