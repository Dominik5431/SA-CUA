#!/usr/bin/env bash
# Make GitLab generate port-less URLs (http://gitlab.com/...) instead of http://gitlab.com:8023/...
#
# Unlike the other replicas, GitLab does not simply serve whatever Host it is given: it renders
# clone URLs, webhook targets and mail links from the external_url baked into /etc/gitlab/gitlab.rb,
# so the port survives in the page even when the request that produced it had none. The standard
# behind-a-reverse-proxy recipe is to drop the port from external_url and pin the port GitLab's own
# nginx listens on, which has to stay 8023 to match the container's published port.
#
# Idempotent, and safe to re-run after the container is recreated from the image (the change lives
# in the container's filesystem, not in gitlab-populated-final-port8023). Takes a few minutes:
# gitlab-ctl reconfigure rewrites the whole configuration and restarts the services.
#
# Usage: bash setup_gitlab_portless_url.sh
set -euo pipefail

CONTAINER="${GITLAB_CONTAINER:-gitlab}"
DOMAIN="${GITLAB_DOMAIN:-gitlab.com}"
LISTEN_PORT="${GITLAB_LISTEN_PORT:-8023}"
RB=/etc/gitlab/gitlab.rb

current=$(docker exec "$CONTAINER" grep -m1 '^external_url' "$RB" || true)
echo "current: ${current:-<none>}"
if [ "$current" = "external_url 'http://${DOMAIN}'" ] \
   && docker exec "$CONTAINER" grep -q "^nginx\['listen_port'\] = ${LISTEN_PORT}$" "$RB"; then
    echo "GitLab already serves http://${DOMAIN} on :${LISTEN_PORT}; nothing to do."
    exit 0
fi

docker exec "$CONTAINER" bash -c "
set -e
[ -f ${RB}.pre-portless ] || cp ${RB} ${RB}.pre-portless
sed -i \"s|^external_url .*|external_url 'http://${DOMAIN}'|\" ${RB}
# Drop any block a previous run appended, then re-add it, so this stays idempotent.
sed -i '/^# --- RedTeamCUA port-less URL ---\$/,/^# --- end RedTeamCUA ---\$/d' ${RB}
cat >> ${RB} <<'RBEOF'
# --- RedTeamCUA port-less URL ---
# external_url above no longer carries a port, so GitLab's nginx would fall back to :80 --
# pin it to the port the container publishes instead.
nginx['listen_port'] = ${LISTEN_PORT}
nginx['listen_https'] = false
# --- end RedTeamCUA ---
RBEOF
"

echo "reconfiguring (this takes a few minutes)..."
docker exec "$CONTAINER" gitlab-ctl reconfigure

echo
docker exec "$CONTAINER" grep -m1 '^external_url' "$RB"
echo "Done. GitLab now renders http://${DOMAIN}/... and still listens on :${LISTEN_PORT}."
