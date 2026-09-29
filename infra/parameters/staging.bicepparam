// Staging environment. Target resource group: finance-sandbox.
//
//   az deployment group create \
//     --resource-group finance-sandbox \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/staging.bicepparam
//
// Secret *names* only. No value in this file (root CLAUDE.md 5.2).

using '../main.bicep'

param appEnv = 'staging'

// Staging always talks to the Stax sandbox.
param staxEnv = 'sandbox'

// Staging owns its own vault; nothing is shared with live (docs/CLAUDE.md Rule 6).
param createKeyVault = true
param keyVaultName = 'finance-sandbox-kv'

param staxApiKeySecretName = 'stax-api-key-sandbox'
param staxWebhookUrlTokenSecretName = 'stax-webhook-url-token-staging'

// Uncomment once the sentry-dsn secret exists in the vault. While it is empty,
// SENTRY_DSN is not set on the app at all and Sentry stays off.
// param sentryDsnSecretName = 'sentry-dsn'

// appBaseUrl is derived: it becomes https://<customHostname> when a custom
// hostname is set, and the default ingress host name otherwise. Override it
// only if the app is fronted by something else, such as a CDN or WAF.
// param appBaseUrl = ''

// ---------------------------------------------------------------------------
// Custom hostname (pending owner confirmation of the name itself)
// ---------------------------------------------------------------------------
// The public zone monvera.io already exists in resource group finance-prod.
param customHostname = 'staging.monvera.io'
param dnsZoneName = 'monvera.io'
param dnsZoneResourceGroup = 'finance-prod'

// DNS writes are a Publish action (root CLAUDE.md section 6). Leave this false:
// the owner flips it to true only for one authorised run, after approving the
// Publish Request, or creates the two records by hand (infra/README.md).
param createDnsRecords = false

// Pass 2 of the hostname setup. Leave false until the managed certificate has
// been issued, then set it to true and redeploy to switch the binding to TLS.
param bindManagedCertificate = false

// ---------------------------------------------------------------------------
// Redis
// ---------------------------------------------------------------------------
// Azure Cache for Redis no longer accepts new instances, so it is not deployed.
// Nothing reads Redis before Step 10, which chooses the replacement (Azure
// Managed Redis, or a Redis container app in this environment).
param deployRedis = false
