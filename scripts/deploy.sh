#!/usr/bin/env bash
#
# One-time (and repeatable) deployment of the Helpdesk stack on a server.
#
# Takes a box that already has Docker from nothing to a running Helpdesk:
# builds the image if it is missing, brings the stack up, waits for site
# creation to actually finish, and fails loudly if the frontend never answers.
#
# Safe to re-run. `create-site` skips a site that already exists, and the
# named volumes survive, so a second run is a restart rather than a reinstall.
#
# Usage:
#   ./scripts/deploy.sh                 # paths relative to this repo
#
#   # Server layout where the compose file and env live in subdirectories:
#   COMPOSE_FILE=/root/helpdesk/docker/docker-compose.helpdesk.yml \
#   ENV_FILE=/root/helpdesk/config/.env.dev \
#   /root/helpdesk/scripts/deploy.sh
#
# Options (environment variables):
#   COMPOSE_FILE   compose file to deploy          (default: repo root)
#   ENV_FILE       environment file to read        (default: repo root .env)
#   FORCE_BUILD=1  rebuild the image even if it is already present
#   SKIP_BUILD=1   never build; the image must already exist or be pullable
#   SKIP_MIGRATE=1 do not run `bench migrate` after the containers start

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

COMPOSE_FILE="${COMPOSE_FILE:-${ROOT}/docker-compose.helpdesk.yml}"
ENV_FILE="${ENV_FILE:-${ROOT}/.env}"

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

# --- preflight --------------------------------------------------------------

step "Preflight"

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose v2 is not available (this needs 'docker compose', not 'docker-compose')"
command -v curl >/dev/null || die "curl is not installed"

[ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE  (copy .env.example and fill it in)"

# Read a value from the env file without sourcing it: sourcing would execute
# whatever is in there, and a password containing a backtick or $( would do
# something a lot more interesting than being assigned to a variable.
env_get() {
  sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n 1 | sed 's/[[:space:]]*$//'
}

IMAGE="$(env_get HELPDESK_IMAGE)"; IMAGE="${IMAGE:-sis/frappe-helpdesk}"
TAG="$(env_get HELPDESK_TAG)";     TAG="${TAG:-v15}"
PORT="$(env_get HELPDESK_PORT)";   PORT="${PORT:-8093}"
SITE_NAME="$(env_get SITE_NAME)"

[ -n "$SITE_NAME" ] || die "SITE_NAME is not set in $ENV_FILE"
[ -n "$(env_get ADMIN_PASSWORD)" ] || die "ADMIN_PASSWORD is not set in $ENV_FILE"
[ -n "$(env_get DB_ROOT_PASSWORD)" ] || die "DB_ROOT_PASSWORD is not set in $ENV_FILE"

echo "    compose file : $COMPOSE_FILE"
echo "    env file     : $ENV_FILE"
echo "    image        : ${IMAGE}:${TAG}"
echo "    site         : $SITE_NAME"
echo "    port         : $PORT"

# Warnings, not failures — a deliberate localhost deploy is legitimate.
case "$(env_get ADMIN_PASSWORD)$(env_get DB_ROOT_PASSWORD)" in
  *change-me*) echo "    WARNING: a password is still set to a change-me placeholder" ;;
esac
case "$SITE_NAME" in
  *.localhost|localhost)
    echo "    WARNING: SITE_NAME is a localhost name. It is the Host header Frappe"
    echo "             routes on and is baked in at site creation — on a real server"
    echo "             this should be the hostname you will actually serve from."
    ;;
esac

COMPOSE=(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE")

# Catches schema problems before anything is started or torn down.
"${COMPOSE[@]}" config -q || die "compose file failed validation"

# --- image ------------------------------------------------------------------

if [ -n "${SKIP_BUILD:-}" ]; then
  step "Skipping build (SKIP_BUILD set)"
  docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1 \
    || docker pull "${IMAGE}:${TAG}" \
    || die "${IMAGE}:${TAG} is neither present locally nor pullable"
elif [ -n "${FORCE_BUILD:-}" ] || ! docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1; then
  # Helpdesk ships in no official image, so there is nothing to pull and no
  # Dockerfile in this repo: build-image.sh layers the app onto frappe_docker's
  # upstream Containerfile. This is the slow part — 15-30 minutes, and it
  # compiles frontend assets, so a 2 GB box will struggle.
  step "Building ${IMAGE}:${TAG} (this takes a while)"
  [ -x "${ROOT}/build-image.sh" ] || [ -f "${ROOT}/build-image.sh" ] \
    || die "build-image.sh not found at ${ROOT} — set SKIP_BUILD=1 to deploy an existing image"
  IMAGE="$IMAGE" TAG="$TAG" bash "${ROOT}/build-image.sh"
else
  step "Image ${IMAGE}:${TAG} already present (FORCE_BUILD=1 to rebuild)"
fi

# --- start ------------------------------------------------------------------

step "Starting the stack"
"${COMPOSE[@]}" up -d

# create-site is a one-shot job. Waiting on it matters: `up -d` returns as soon
# as the containers are created, and on a first run the site does not exist for
# several minutes after that. Without this wait, the health check below fails
# against a stack that was merely still working.
step "Waiting for site creation"
cid="$("${COMPOSE[@]}" ps -qa create-site 2>/dev/null | tail -n 1)"
if [ -n "$cid" ]; then
  for _ in $(seq 1 120); do
    status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo missing)"
    if [ "$status" = "exited" ]; then
      code="$(docker inspect -f '{{.State.ExitCode}}' "$cid")"
      if [ "$code" != "0" ]; then
        echo "create-site failed (exit $code):" >&2
        docker logs --tail 60 "$cid" >&2
        exit 1
      fi
      docker logs --tail 5 "$cid"
      break
    fi
    sleep 5
  done
else
  echo "    create-site container not found — assuming an existing site."
fi

# --- migrate ----------------------------------------------------------------

# A rebuilt image can carry schema changes even though the site itself, living
# in a named volume, survived untouched.
if [ -z "${SKIP_MIGRATE:-}" ]; then
  step "Running bench migrate"
  "${COMPOSE[@]}" exec -T backend bench --site all migrate
fi

# --- verify -----------------------------------------------------------------

step "Waiting for the frontend"
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:${PORT}/api/method/ping" >/dev/null 2>&1; then
    echo
    echo "Helpdesk is up."
    echo "    Agent UI : http://localhost:${PORT}/helpdesk"
    echo "    Desk     : http://localhost:${PORT}/app   (Administrator / ADMIN_PASSWORD)"
    echo
    echo "Next, if this is a first install:"
    echo "  1. Create the SIS integration user and generate its API keys (docs/helpdesk-setup.md step 3)"
    echo "  2. Fill ADMIN_API_KEY / ADMIN_API_SECRET in ${ENV_FILE}"
    echo "  3. python scripts/provision.py --dry-run   then without the flag"
    echo "  4. Point the SIS at this stack and enable the 'support' waffle switch"
    exit 0
  fi
  sleep 5
done

# configurator and create-site showing Exited is correct; anything else is not.
echo "Frontend did not answer on port ${PORT} within 300s." >&2
"${COMPOSE[@]}" ps
"${COMPOSE[@]}" logs --tail 100 backend frontend
exit 1
