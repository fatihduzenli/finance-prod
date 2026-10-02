// The `worker` container app: the single process that calls Stax
// (docs/qa/PLAN-010-worker-and-reconciliation.md D2, D4).
//
// Same image, identity, vault references and environment as the web app, plus
// STAX_CALLER=worker. It runs `python backend/manage.py worker`, which drains the
// `jobs` table and the stored webhook events one item at a time. Exactly one
// replica (min and max 1): the rate limiter, breaker and failure budget live in
// this one process. No ingress: nothing calls it. Migrations stay with the web
// app's start; this start runs only the boot check and then the worker.
//
// No secret value appears in this template, in a parameter file or in an output;
// only secret *names* do (root CLAUDE.md 5.3).

param location string
param appName string

@description('Resource ID of the Container Apps environment.')
param environmentId string

@description('Workload profile to run on.')
param workloadProfileName string = 'Consumption'

@description('Fully qualified image reference: the same image as the web app. CI replaces it with the built image.')
param image string

@description('Resource ID of the user-assigned managed identity used for registry pulls and vault reads.')
param identityResourceId string

@description('Login server of the container registry, e.g. myregistry.azurecr.io.')
param registryLoginServer string

@description('Vault URI, with a trailing slash, e.g. https://finance-kv.vault.azure.net/.')
param keyVaultUri string

@allowed([
  'staging'
  'production'
])
param appEnv string

@allowed([
  'sandbox'
  'live'
])
param staxEnv string

param staxApiBaseUrl string

@description('STAX_EXPECTED_MERCHANT_ID: the merchant the Stax key must belong to. An identifier, not a secret. Empty means not checked.')
param staxExpectedMerchantId string = ''

@description('STAX_MAX_TXN_AMOUNT: the ACH per-transaction limit confirmed by Stax (PLAN-007c). Not a secret. Empty means unknown: no hard block.')
param staxMaxTxnAmount string = ''

@description('Public base URL of the web app. The environment guard requires it in every process.')
param appBaseUrl string

@description('Vault secret name for the Stax API key, e.g. stax-api-key-sandbox.')
param staxApiKeySecretName string

@description('Vault secret name for the inbound webhook URL token.')
param staxWebhookUrlTokenSecretName string

@description('Vault secret name for DATABASE_URL.')
param databaseUrlSecretName string

@description('Vault secret name for the Django secret key.')
param djangoSecretKeySecretName string

@description('Vault secret name for the Sentry DSN. Leave empty to run without Sentry; the env var is then not set at all.')
param sentryDsnSecretName string = ''

@description('CPU cores as a string, converted with json(). Must pair with memory per the Container Apps allowed combinations.')
param cpuCores string = '0.5'

param memory string = '1Gi'

param tags object = {}

var wireSentry = !empty(sentryDsnSecretName)

var baseSecrets = [
  {
    name: 'stax-api-key'
    keyVaultUrl: '${keyVaultUri}secrets/${staxApiKeySecretName}'
    identity: identityResourceId
  }
  {
    name: 'stax-webhook-url-token'
    keyVaultUrl: '${keyVaultUri}secrets/${staxWebhookUrlTokenSecretName}'
    identity: identityResourceId
  }
  {
    name: 'database-url'
    keyVaultUrl: '${keyVaultUri}secrets/${databaseUrlSecretName}'
    identity: identityResourceId
  }
  {
    name: 'django-secret-key'
    keyVaultUrl: '${keyVaultUri}secrets/${djangoSecretKeySecretName}'
    identity: identityResourceId
  }
]

var sentrySecrets = [
  {
    name: 'sentry-dsn'
    keyVaultUrl: '${keyVaultUri}secrets/${sentryDsnSecretName}'
    identity: identityResourceId
  }
]

var baseEnv = [
  {
    name: 'APP_ENV'
    value: appEnv
  }
  {
    name: 'STAX_ENV'
    value: staxEnv
  }
  {
    name: 'STAX_API_BASE_URL'
    value: staxApiBaseUrl
  }
  {
    name: 'STAX_CALLER'
    value: 'worker'
  }
  {
    name: 'STAX_EXPECTED_MERCHANT_ID'
    value: staxExpectedMerchantId
  }
  {
    name: 'STAX_MAX_TXN_AMOUNT'
    value: staxMaxTxnAmount
  }
  {
    name: 'APP_BASE_URL'
    value: appBaseUrl
  }
  {
    name: 'STAX_API_KEY'
    secretRef: 'stax-api-key'
  }
  {
    name: 'STAX_WEBHOOK_URL_TOKEN'
    secretRef: 'stax-webhook-url-token'
  }
  {
    name: 'DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'DJANGO_SECRET_KEY'
    secretRef: 'django-secret-key'
  }
]

var sentryEnv = [
  {
    name: 'SENTRY_DSN'
    secretRef: 'sentry-dsn'
  }
]

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: workloadProfileName
    configuration: {
      activeRevisionsMode: 'Single'
      // No ingress: the worker polls the database and is never called.
      registries: [
        {
          server: registryLoginServer
          identity: identityResourceId
        }
      ]
      secrets: wireSentry ? concat(baseSecrets, sentrySecrets) : baseSecrets
    }
    template: {
      // 60 s, not the 30 s default, after SIGTERM: a job mid-retry (backoff 1 + 4 + 15 s) can finish.
      terminationGracePeriodSeconds: 60
      containers: [
        {
          name: 'worker'
          image: image
          // The boot check (one GET /self, PLAN-006 D2) runs first; any disagreement
          // exits non-zero and the revision never starts. `exec` makes the worker PID 1,
          // so SIGTERM reaches it and it finishes the current item before exiting.
          command: [
            '/bin/sh'
            '-c'
          ]
          args: [
            'python backend/manage.py provider_environment_check && exec python backend/manage.py worker'
          ]
          resources: {
            cpu: json(cpuCores)
            memory: memory
          }
          env: wireSentry ? concat(baseEnv, sentryEnv) : baseEnv
        }
      ]
      scale: {
        // Exactly one replica: one process owns the single Stax rate-limit bucket.
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output appId string = worker.id
output appName string = worker.name
