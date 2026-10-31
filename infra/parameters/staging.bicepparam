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

// From Step 10 the worker container app (finance-staging-worker, one replica,
// STAX_CALLER=worker) is the single Stax caller (docs/qa/PLAN-010-worker-and-
// reconciliation.md D4): the web app only queues jobs and refuses provider calls, so it
// runs with 'none' and scales to the default webMaxReplicas (2).
param staxCaller = 'none'

// The sandbox merchant read from GET /self on 2026-09-30. An identifier, not a secret.
// provider_environment_check refuses to start the app when the key belongs to another.
param staxExpectedMerchantId = 'ea824f2b-90b3-4c49-8f69-ba06943458f4'

// staxMaxTxnAmount (STAX_MAX_TXN_AMOUNT, PLAN-007c) is set here only once Stax answers
// SCOPE Q25 with the confirmed ACH per-transaction limit. Until then it stays empty and
// no debit is hard-blocked.
param staxWebhookUrlTokenSecretName = 'stax-webhook-url-token-staging'

// The sentry-dsn secret exists in the vault since 2026-10-02 (owner); SENTRY_DSN is read
// from it through the managed identity. Alerts: level fatal emails the owner.
param sentryDsnSecretName = 'sentry-dsn'

// The USPS address check (PLAN-012f-c D14): the web app reads USPS_CLIENT_ID and
// USPS_CLIENT_SECRET from the vault secrets usps-client-id and usps-client-secret.
// Uncomment both lines after the secrets exist in finance-sandbox-kv: a reference to a
// missing secret stops the revision from starting. Until then the check is off and the
// store form saves with "Address not verified".
// param uspsClientIdSecretName = 'usps-client-id'
// param uspsClientSecretSecretName = 'usps-client-secret'

// The bank-entry page's hosted fields (PLAN-014 C7): the web app reads
// STAX_WEB_PAYMENTS_TOKEN from the vault secret stax-web-payments-token. Uncomment after
// the secret exists in finance-sandbox-kv; until then bank entry is not set up.
// param staxWebPaymentsTokenSecretName = 'stax-web-payments-token'

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
