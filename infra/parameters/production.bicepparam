// Production environment. Target resource group: finance-prod.
//
//   az deployment group create \
//     --resource-group finance-prod \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/production.bicepparam
//
// Secret *names* only. No value in this file (root CLAUDE.md 5.2).
//
// There is no CI job that deploys production. Step 16 adds a
// workflow_dispatch-only job, and every run is a Publish action that needs
// explicit human authorisation (root CLAUDE.md section 6).

using '../main.bicep'

param appEnv = 'production'

// Checkpoint 2: production runs against the Stax *sandbox* for now. No sandbox
// key exists yet, and the live key stored in finance-kv on 2026-09-16 is not
// wired to anything until Step 16. At that step, and only then, flip staxEnv to
// 'live' and staxApiKeySecretName to 'stax-api-key-live' in the same change.
// The boot guard rejects a live key outside production, and allows sandbox in
// production precisely so this intermediate state is safe (PLAN-002 section 3,
// guard rules 3 and 4).
param staxEnv = 'sandbox'
param staxApiKeySecretName = 'stax-api-key-sandbox'

// The existing vault is referenced, never recreated (ADR-0002).
param createKeyVault = false
param keyVaultName = 'finance-kv'
param keyVaultResourceGroupName = 'finance-prod'

param staxWebhookUrlTokenSecretName = 'stax-webhook-url-token-production'

// Distinct address space from staging, so the two networks could ever be peered.
param vnetAddressPrefix = '10.30.0.0/16'
param infrastructureSubnetPrefix = '10.30.0.0/23'
param postgresSubnetPrefix = '10.30.2.0/24'

// Uncomment once the sentry-dsn secret exists in the vault.
// param sentryDsnSecretName = 'sentry-dsn'

// appBaseUrl is derived: it becomes https://<customHostname> when a custom
// hostname is set, and the default ingress host name otherwise. Override it
// only if the app is fronted by something else, such as a CDN or WAF.
// param appBaseUrl = ''

// ---------------------------------------------------------------------------
// Custom hostname (pending owner confirmation of the name itself)
// ---------------------------------------------------------------------------
// The public zone monvera.io already exists in this resource group.
param customHostname = 'app.monvera.io'
param dnsZoneName = 'monvera.io'
param dnsZoneResourceGroup = 'finance-prod'

// DNS writes are a Publish action (root CLAUDE.md section 6). Leave this false:
// the owner flips it to true only for one authorised run, after approving the
// Publish Request, or creates the two records by hand (infra/README.md).
param createDnsRecords = false

// Pass 2 of the hostname setup. Leave false until the managed certificate has
// been issued, then set it to true and redeploy to switch the binding to TLS.
param bindManagedCertificate = false
