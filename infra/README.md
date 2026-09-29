# infra — Azure environment for the invoicing app

Bicep templates for the two cloud environments described in
[`docs/DECISIONS.md`](../docs/DECISIONS.md) ADR-0002 and
[`docs/qa/PLAN-002-scaffolding.md`](../docs/qa/PLAN-002-scaffolding.md) §3.

```
infra/
  main.bicep                       # one resource group per environment
  modules/
    network.bicep                  # VNet, delegated subnets, NAT gateway (static egress IP), private DNS zone
    monitoring.bicep               # Log Analytics workspace
    containerAppsEnvironment.bicep # Container Apps environment on the delegated subnet
    registry.bicep                 # Azure Container Registry (Basic), admin user off
    identity.bicep                 # user-assigned managed identity + AcrPull
    keyVault.bicep                 # created for staging only
    keyVaultAccess.bicep           # Key Vault Secrets User for that identity
    dnsRecords.bicep               # CNAME + asuid TXT in the monvera.io zone (opt-in)
    managedCertificate.bicep       # free managed TLS certificate for the custom hostname
    postgres.bicep                 # PostgreSQL Flexible Server 16, Burstable B1ms, private access
    postgresSecrets.bicep          # writes database-url and postgres-admin-password into the vault
    redis.bicep                    # Azure Cache for Redis: retired for new instances, off by default (deployRedis)
    containerAppWeb.bicep          # the web container app, secrets as Key Vault references
  parameters/
    staging.bicepparam             # resource group finance-sandbox
    production.bicepparam          # resource group finance-prod
```

**Every command in this file that creates, changes or deletes an Azure resource,
a credential or a GitHub setting is a Publish action under root
[`CLAUDE.md`](../CLAUDE.md) §6. A model may not run any of them.** They are
written here for the owner to run, in this order, and each needs explicit
authorisation in the moment. `what-if` dry runs are safe and are used below.

Nothing in this directory contains a secret value. Secrets are referenced by
**name** in Key Vault and resolved at container start by the managed identity.

---

## 0. Prerequisites

- Azure CLI signed in to the owner's subscription, plus the Bicep tooling
  (`az bicep install`).
- Permission to create resources **and** role assignments in the subscription
  (Owner, or Contributor plus User Access Administrator).
- The existing resource group `finance-prod` with the vault `finance-kv`
  (ADR-0002). The staging resource group does not exist yet.
- `finance-kv` must have **RBAC authorisation enabled**. These templates grant
  access by role assignment; a vault still using access policies would hand the
  app an authorisation error at container start. Check it:

  ```bash
  az keyvault show --name finance-kv --query properties.enableRbacAuthorization
  ```

> **Subscription and region facts (recorded 2026-09-28).**
> - The subscription `Azure subscription 1` is **Pay-As-You-Go** with the spending limit off
>   (verified from the CLI on 2026-09-28: quota ID `PayAsYouGo_2014-09-01`). It was a Free Trial
>   when staging was first attempted and was upgraded the same day. **Production must never be
>   deployed to a trial subscription**: a trial's spending limit switches every resource off when
>   the credit is exhausted, the trial ends after 30 days, and trials are refused in several regions.
> - **East US was rejected while the subscription was a trial**: PostgreSQL Flexible Server
>   reported "provisioning is restricted in this region" and the Container Apps environment failed
>   with an AKS capacity error. East US 2, South Central US, West US 2, West Europe and Canada
>   East refused PostgreSQL as well. **Staging runs in Central US by owner decision; East US is not
>   to be retried.** Region-bound resources created in East US were deleted on 2026-09-28. The
>   vault, registry, identity and private DNS zone were kept; they sit in East US and are
>   region-independent for this workload.
> - Pass `location=centralus` on every `az deployment group create` for staging, because the
>   template defaults `location` to the resource group's location, which is East US.
> - The staging vault was created with purge protection **off** so the environment can be torn
>   down and rebuilt. Deploy `main.bicep` with `createKeyVault=false` for staging so the vault is
>   referenced, not recreated with the module default. Production's `finance-kv` is never created
>   by the template.

## 1. Create the resource groups

```bash
az group create --name finance-sandbox --location eastus
# finance-prod already exists; do not recreate it.
```

Use one region for everything; `eastus` is assumed above. If you pick another,
pass `--location` to the deployments as well, and keep both environments in the
same region as their vault.

## 2. Create the vault and set the secrets — before anything else

**Order matters.** The web container app declares its secrets as Key Vault
references, and Azure resolves them while the first revision is created. If a
referenced secret does not exist yet, the deployment in §3 fails. So for staging,
deploy the vault module on its own first:

```bash
az deployment group create \
  --resource-group finance-sandbox \
  --template-file infra/modules/keyVault.bicep \
  --parameters keyVaultName=finance-sandbox-kv location=eastus
```

Production skips this: `finance-kv` already exists and is never recreated.

The vaults use RBAC, so being the subscription owner is not by itself enough to
write a secret. Grant yourself **Key Vault Secrets Officer** once per vault:

```bash
az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee "<your-user-object-id>" \
  --scope "$(az keyvault show --name finance-sandbox-kv --query id --output tsv)"
```

Then set the secrets **by name**. Never put a value in this file, in a parameter
file, in a commit message or in a chat. Prefer `--file` over `--value` so the
value does not land in shell history, and delete the file afterwards:

```bash
# Write the value to a file OUTSIDE the repository, use it, then destroy it.
az keyvault secret set --vault-name finance-sandbox-kv \
  --name stax-api-key-sandbox --file "$HOME/stax-sandbox-key.txt"
rm -P "$HOME/stax-sandbox-key.txt"   # GNU coreutils: shred -u
```

| Secret name | Vault | Who sets it | Notes |
|---|---|---|---|
| `stax-api-key-sandbox` | `finance-sandbox-kv` and `finance-kv` | owner | **Does not exist yet.** Until it does, no session and no environment may make a Stax call at all (`docs/CLAUDE.md` §4). Production references this one too, until Step 16. |
| `stax-api-key-live` | `finance-kv` | already stored 2026-09-16 | Wired to nothing until Step 16. No development identity may read it. |
| `stax-webhook-url-token-staging` | `finance-sandbox-kv` | owner | ≥ 64 URL-safe characters, unique per environment. E.g. `openssl rand -base64 96 \| tr -dc 'A-Za-z0-9_-' \| cut -c1-72`. |
| `stax-webhook-url-token-production` | `finance-kv` | owner | As above, a different value. |
| `django-secret-key` | both | owner | ≥ 50 random characters, unique per environment. |
| `sentry-dsn` | both | owner, optional | Only after this exists may you uncomment `sentryDsnSecretName` in the parameter file and redeploy. While it is unset, `SENTRY_DSN` is not passed to the app at all and Sentry stays off. |
| `database-url` | both | **the deployment in §3** | Composed from the generated database password. Do not set it by hand; a manual value is overwritten on the next deployment. |
| `postgres-admin-password` | both | **the deployment in §3** | The generated server administrator password, for operator use. |
| `redis-url` | both | owner, at Step 10 | Nothing reads it yet, and no Redis is deployed today (§10). Its source is chosen at Step 10; whatever it is, no model may read or print the access key. |

Required before §3 succeeds: the Stax API key secret, the webhook URL token, and
the Django secret key for that environment. `database-url` is created by the
deployment itself, in the same run, before the container app is created.

## 3. Dry run, then deploy the environment

Staging:

```bash
az deployment group what-if \
  --resource-group finance-sandbox \
  --template-file infra/main.bicep \
  --parameters infra/parameters/staging.bicepparam

az deployment group create \
  --resource-group finance-sandbox \
  --template-file infra/main.bicep \
  --parameters infra/parameters/staging.bicepparam
```

Production uses the same template with the other parameter file and has **no**
automatic deployment job (§8):

```bash
az deployment group what-if \
  --resource-group finance-prod \
  --template-file infra/main.bicep \
  --parameters infra/parameters/production.bicepparam

az deployment group create \
  --resource-group finance-prod \
  --template-file infra/main.bicep \
  --parameters infra/parameters/production.bicepparam
```

If a custom hostname is set in the parameter file — both files ship with one —
**read §4 before running the create above**: the hostname binding and the managed
certificate have an ordering requirement that governs which deployment you run
first.

What this deployment does **not** do:

- It does not build an image. The web app starts on a public placeholder image,
  and its revision reports unhealthy until CI pushes the real one (§7). That is
  expected on day one.
- It does not create the GitHub federated credential (§5) or the repository
  variables (§6).
- It does not create resource groups, and it never creates `finance-kv`.

Record these outputs; §4–§9 need them:

```bash
az deployment group show \
  --resource-group finance-sandbox --name main \
  --query properties.outputs
```

| Output | Used for |
|---|---|
| `natEgressPublicIpAddress` | the static egress IP to give Stax (§9) |
| `registryName` | GitHub variable `ACR_NAME` |
| `webAppName` | GitHub variable `WEB_APP_NAME` |
| `appBaseUrl` | GitHub variable `APP_BASE_URL` |
| `keyVaultName`, `keyVaultUri` | where the secrets from §2 live |
| `managedIdentityPrincipalId` | granting the app access to anything else later |
| `postgresServerFqdn`, `postgresDatabaseName` | operations |
| `redisHostName` | empty unless `deployRedis=true` (§10) |
| `defaultIngressFqdn` | the CNAME target for a custom hostname (§4) |
| `customDomainVerificationId` | the value of the `asuid.<label>` TXT record (§4) |
| `customHostname`, `customDomainBindingType` | what the app is bound to, and whether TLS is live (§4) |

**Redeployment caveat.** The database administrator password is generated fresh on
every `az deployment group create` unless you pass `postgresAdministratorPassword`
yourself. A redeployment therefore rotates it and refreshes `database-url` in the
same run, but a running container keeps the old value until it restarts:

```bash
RG=finance-sandbox
APP="<webAppName>"
az containerapp revision restart --name "$APP" --resource-group "$RG" \
  --revision "$(az containerapp show --name "$APP" --resource-group "$RG" \
    --query properties.latestRevisionName --output tsv)"
```

## 4. Custom domains and DNS

The public zone **`monvera.io`** already exists in resource group
`finance-prod`. Proposed hostnames, **pending the owner's confirmation**:

| Environment | Hostname | Parameter file |
|---|---|---|
| Staging | `staging.monvera.io` | `infra/parameters/staging.bicepparam` |
| Production | `app.monvera.io` | `infra/parameters/production.bicepparam` |

Both files set `customHostname` already, with `createDnsRecords = false` and
`bindManagedCertificate = false`. Setting `customHostname` to `''` disables the
whole custom-domain path and the app is reachable on its default ingress host
name only.

### Why this is two passes

Azure enforces an order that cannot be expressed as one deployment:

1. The `<label>` CNAME and the `asuid.<label>` TXT records must resolve.
2. The hostname must be bound to a container app in the environment, **without**
   a certificate (`bindingType: Disabled`) — that binding is what the managed
   certificate is issued against.
3. Only then can the environment issue the managed certificate.
4. The binding is then updated to `bindingType: SniEnabled` with the certificate
   attached, which is when HTTPS on the custom hostname starts working.

`main.bicep` therefore orders DNS → web app (hostname, `Disabled`) → certificate
request inside one deployment, and leaves step 4 to a second deployment gated on
`bindManagedCertificate`.

### Path A — let the deployment write the records

One authorised run with `createDnsRecords = true`. **Writing DNS is a Publish
action under root [`CLAUDE.md`](../CLAUDE.md) §6:** the owner approves that exact
run, a model does not flip this flag.

```bash
# Pass 1: records, hostname bound without a certificate, certificate requested.
az deployment group create \
  --resource-group finance-sandbox \
  --template-file infra/main.bicep \
  --parameters infra/parameters/staging.bicepparam \
  --parameters createDnsRecords=true

# Confirm the certificate reached Succeeded (usually a few minutes).
az containerapp env certificate list \
  --name finance-staging-cae --resource-group finance-sandbox \
  --query "[].{name:name,state:properties.provisioningState,subject:properties.subjectName}" \
  --output table

# Pass 2: attach it and switch the binding to TLS.
az deployment group create \
  --resource-group finance-sandbox \
  --template-file infra/main.bicep \
  --parameters infra/parameters/staging.bicepparam \
  --parameters createDnsRecords=true bindManagedCertificate=true
```

If pass 1 fails on certificate validation, the records had not propagated yet.
Wait a few minutes and re-run the same command: the deployment is idempotent, and
everything created before the failure stays.

### Path B — write the records by hand

Use this when the owner would rather not grant DNS write permission to the
deployment, or wants to see the records before they exist.

First deploy **with `customHostname` commented out** in the parameter file, so
that no hostname binding is attempted, and read the two values you need:

```bash
az deployment group show --resource-group finance-sandbox --name main \
  --query "{target:properties.outputs.defaultIngressFqdn.value, \
            asuid:properties.outputs.customDomainVerificationId.value}"
```

Then create the records in the zone's resource group:

```bash
az network dns record-set cname set-record \
  --resource-group finance-prod --zone-name monvera.io \
  --record-set-name staging --cname "<defaultIngressFqdn>" --ttl 300

az network dns record-set txt add-record \
  --resource-group finance-prod --zone-name monvera.io \
  --record-set-name asuid.staging --value "<customDomainVerificationId>"
```

Check them, then continue with pass 1 and pass 2 from Path A, leaving
`createDnsRecords = false`:

```bash
dig +short staging.monvera.io CNAME
dig +short asuid.staging.monvera.io TXT
```

For production the same commands apply with `--record-set-name app`,
`asuid.app`, and the `finance-prod` values.

### Permissions

The **deploy identity needs `DNS Zone Contributor` on the zone only if
`createDnsRecords` is used** — that is, only for Path A, and only for whoever
runs that deployment. GitHub Actions never needs it: CI only builds an image and
updates the container app (§5). Grant it scoped to the zone, not the resource
group:

```bash
az role assignment create \
  --role "DNS Zone Contributor" \
  --assignee "<object-id-of-whoever-runs-path-A>" \
  --scope "$(az network dns zone show --name monvera.io --resource-group finance-prod --query id --output tsv)"
```

### Consequences for the application

- `APP_BASE_URL` becomes `https://<customHostname>` as soon as `customHostname`
  is set, because that is what the app must consider its own host. Django's
  `ALLOWED_HOSTS` is derived from it, so between pass 1 and pass 2 the app answers
  on the custom hostname and returns `400` to a request that arrives with the
  default ingress host name. Health probes are unaffected: they send the custom
  host explicitly.
- Set the GitHub variable `APP_BASE_URL` to `https://<customHostname>` **after**
  pass 2, when TLS is live (§6). Until then leave it on the default ingress host
  name, or the deployment smoke test will fail on certificate validation.
- The `web` app's ingress keeps serving its default host name as well. Nothing
  redirects; if a single canonical host is wanted later, that is a Django-level
  decision, not an infrastructure one.
- Certificate renewal is automatic for managed certificates as long as the CNAME
  keeps pointing at the app.

## 5. Create the GitHub OIDC federated credential

CI signs in to Azure with a federated token, so no client secret exists anywhere.

```bash
# 1. App registration for the deploy identity.
az ad app create --display-name "finance-deploy-staging"
APP_ID="$(az ad app list --display-name finance-deploy-staging --query '[0].appId' --output tsv)"
az ad sp create --id "$APP_ID"

# 2. Federated credential. The subject must match the workflow exactly: the
#    deploy-staging job runs on a push to main with `environment: staging`.
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-main-staging",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<github-owner>/<repo>:environment:staging",
  "audiences": ["api://AzureADTokenExchange"]
}'

# 3. Least-privilege roles: push images, and update the web container app.
SP_ID="$(az ad sp list --display-name finance-deploy-staging --query '[0].id' --output tsv)"
az role assignment create --role AcrPush \
  --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
  --scope "$(az acr show --name "<registryName>" --query id --output tsv)"
az role assignment create --role Contributor \
  --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
  --scope "$(az containerapp show --name "<webAppName>" --resource-group finance-sandbox --query id --output tsv)"
```

The deploy identity gets **no** Key Vault role. It never reads a secret; the
app's own managed identity does that at runtime.

A production deployment at Step 16 needs its own app registration, its own
federated credential with subject
`repo:<owner>/<repo>:environment:production`, and a GitHub `production`
environment with a required reviewer. Do not reuse the staging identity.

## 6. Set the GitHub repository variables and secrets

Create the GitHub **environment** named `staging` first
(`Settings → Environments`): the deploy job targets it and the federated
credential subject in §5 refers to it.

Variables (`Settings → Secrets and variables → Actions → Variables`) — not
secrets, just names the workflow interpolates:

| Variable | Value |
|---|---|
| `ACR_NAME` | deployment output `registryName` |
| `WEB_APP_NAME` | deployment output `webAppName` |
| `RESOURCE_GROUP` | `finance-sandbox` |
| `APP_BASE_URL` | deployment output `appBaseUrl`, no trailing slash. With a custom hostname, set this only after pass 2 in §4; until then use `defaultIngressFqdn`. |

Secrets (`… → Secrets`) — Azure identifiers rather than credentials, but they
belong in the secret store so they stay out of logs:

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | `appId` of the `finance-deploy-staging` app registration |
| `AZURE_TENANT_ID` | `az account show --query tenantId --output tsv` |
| `AZURE_SUBSCRIPTION_ID` | `az account show --query id --output tsv` |

With the `gh` CLI — the owner runs these, a model may not:

```bash
gh variable set ACR_NAME --body "<registryName>"
gh variable set WEB_APP_NAME --body "<webAppName>"
gh variable set RESOURCE_GROUP --body "finance-sandbox"
gh variable set APP_BASE_URL --body "https://<webAppFqdn>"
gh secret set AZURE_CLIENT_ID        # prompts, so the value stays out of history
gh secret set AZURE_TENANT_ID
gh secret set AZURE_SUBSCRIPTION_ID
```

## 7. First real image

`.github/workflows/ci.yml` does this on every push to `main` after the test job
passes. The same two commands roll an image by hand:

```bash
TAG="$(git rev-parse HEAD)"
az acr build --registry "<registryName>" --image "finance-web:$TAG" .
az containerapp update --name "<webAppName>" --resource-group finance-sandbox \
  --image "<registryName>.azurecr.io/finance-web:$TAG"
# Use whatever the app's own APP_BASE_URL is: the custom hostname once §4 pass 2
# is done, the default ingress host name before that. Any other host gets a 400.
curl --fail --silent "$(az deployment group show --resource-group finance-sandbox \
  --name main --query properties.outputs.appBaseUrl.value --output tsv)/healthz"
```

`/healthz` answering `"stax_env":"sandbox"` is the signal that the environment
guard and every Key Vault reference resolved. `/admin/login/` must show the
undismissable yellow SANDBOX bar, with `[SANDBOX]` in the page title
(`docs/CLAUDE.md` Rule 6).

## 8. Production deployments

There is deliberately no production job in the CI workflow. Step 16 adds a
`workflow_dispatch`-only job with a protected `production` environment and a
required reviewer. Until then, production is deployed by the owner running §7's
commands against `finance-prod`, as an authorised Publish action.

Production currently runs `STAX_ENV=sandbox` with
`staxApiKeySecretName=stax-api-key-sandbox` (Checkpoint 2). Switching to the live
key is one deliberate change at Step 16: set `staxEnv = 'live'` **and**
`staxApiKeySecretName = 'stax-api-key-live'` in
`infra/parameters/production.bicepparam`, together, and redeploy. The boot guard
rejects a live key anywhere but production and accepts sandbox in production,
precisely so that this intermediate state cannot send a real invoice.

## 9. Giving Stax the static egress IP

All outbound traffic from the Container Apps environment leaves through the NAT
gateway, so the whole environment shares one fixed public address:

```bash
az deployment group show --resource-group finance-sandbox --name main \
  --query properties.outputs.natEgressPublicIpAddress.value --output tsv
```

Send it to Stax support against the open whitelisting question in
[`docs/TODO-VERIFY.md`](../docs/TODO-VERIFY.md). Worth stating in the ticket: it
is a static Standard-SKU address; it does not change when the app is redeployed
or scaled; and the worker and beat apps added at Step 10 will share it because
they run in the same environment. Staging and production have separate NAT
gateways and therefore **two** addresses — send both, labelled.

If whitelisting turns out not to be worth it, the NAT gateway is the single
largest line item here (~$35/month per environment, ADR-0002). Removing it is a
change to `network.bicep`, at the cost of a dynamic egress IP.

## 10. Step 10 (Celery) is already covered by this platform

Nothing has to be re-planned for the worker and beat:

- **Always-on replicas.** Container Apps runs an app with `minReplicas: 1` and no
  ingress, which is exactly what a Celery worker and a single-replica beat need.
  They reuse this image, this environment, this managed identity and these Key
  Vault references, with a different command. Their place is marked with a
  comment in `main.bicep`.
- **Scheduled work.** Container Apps **Jobs** run on a cron schedule
  (`--trigger-type Schedule`), a separate resource type that is an alternative to
  a persistent Celery beat for the nightly reconciliation.
- **Redis is not provisioned.** Azure Cache for Redis (`Microsoft.Cache/redis`),
  which `modules/redis.bicep` deploys, no longer accepts new instances: on
  2026-09-28 Azure refused it with "Azure Cache for Redis is retiring, create
  Azure Managed Redis instance instead". The template therefore skips it
  (`deployRedis = false`). Step 10 chooses the replacement for the broker, the
  Stax token bucket and the circuit-breaker window (`docs/CLAUDE.md` Rule 3):
  Azure Managed Redis, or a Redis container app in this environment. Nothing
  reads Redis before then.

## 11. Cost note

Per ADR-0002, roughly: Container Apps ~$30, PostgreSQL Burstable B1ms ~$15–25,
NAT gateway ~$35, registry and vault a few dollars. Staging
is the same shape. If that is more than the sandbox is worth, the cheapest
reductions are stopping the staging PostgreSQL server when it is idle, then
dropping the staging NAT gateway and accepting a dynamic egress IP there.
