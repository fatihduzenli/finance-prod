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

// Until Step 10 the web process is the single Stax caller (docs/qa/PLAN-006-operators.md
// D1): the operators screens create and link Stax customers. Stax's rate limit is per
// source IP and the limiter is per process, so the web app must then run as ONE
// replica. Step 10 moves every Stax call to the worker, sets staxCaller back to 'none'
// and removes webMaxReplicas = 1. Do not raise the replica count before then.
param staxCaller = 'worker'
param webMaxReplicas = 1

// The sandbox merchant read from GET /self on 2026-09-30. An identifier, not a secret.
// provider_environment_check refuses to start the app when the key belongs to another.
param staxExpectedMerchantId = 'ea824f2b-90b3-4c49-8f69-ba06943458f4'

// staxMaxTxnAmount (STAX_MAX_TXN_AMOUNT, PLAN-007c) is set here only once Stax answers
// SCOPE Q25 with the confirmed ACH per-transaction limit. Until then it stays empty and
// no debit is hard-blocked.
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
