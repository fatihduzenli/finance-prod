#!/bin/bash
# Is staging on commit $1 (the full sha)? READ-ONLY. Prints the count of active revisions of the web and
# worker apps whose image tag is the sha, whether each job's image is on it, and /healthz. A line ending
# ": 0" or ": NO" means the commit is not live there. Resource names: the staging environment of
# infra/ (override STAGING_RG and STAGING_URL for another).
SHA="${1:?full commit sha}"; RG="${STAGING_RG:-finance-sandbox}"; URL="${STAGING_URL:-https://staging.monvera.io}"
for app in finance-staging-web finance-staging-worker; do
  n=$(az containerapp revision list -n "$app" -g "$RG" --query "length([?properties.active && ends_with(properties.template.containers[0].image, ':$SHA')])" -o tsv 2>/dev/null)
  echo "$app live revisions on ${SHA:0:7}: ${n:-0}"
done
for job in finance-staging-reconcile finance-staging-daily; do
  img=$(az containerapp job show -n "$job" -g "$RG" --query "properties.template.containers[0].image" -o tsv 2>/dev/null)
  case "$img" in *:"$SHA") echo "$job on ${SHA:0:7}: yes";; *) echo "$job on ${SHA:0:7}: NO";; esac
done
echo "healthz: $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$URL/healthz")"
