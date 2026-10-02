// The `reconcile` scheduled job: it queues the nightly reconciliation for the worker
// (docs/qa/PLAN-010-worker-and-reconciliation.md D5 to D9).
//
// A Container Apps job with a Schedule trigger. Container Apps cron is UTC only, so it
// fires at 06:00 and 07:00 UTC; `reconcile --scheduled` acts only on the firing that is
// 02:00 local time (RECONCILE_LOCAL_TIME, RECONCILE_TIMEZONE) and records the other one
// as skipped, so daylight saving time never moves or doubles the run. When due it only
// queues a `reconcile_run` job (trigger scheduled) in the database and exits; the
// worker runs it. The job never calls Stax: STAX_CALLER=none, no provider boot check,
// and the worker stays the one process with the rate limiter, the breaker and the
// budget (docs/CLAUDE.md Rule 3). One replica, no retry: a firing that failed to queue
// is retried by the next one inside the window, or the next night redoes the window.
// Same image, identity and vault references as the worker: the environment guard
// requires every one of them in every process, so they stay although the job uses only
// the database.
//
// No secret value appears in this template, in a parameter file or in an output;
// only secret *names* do (root CLAUDE.md 5.3).

param location string
@description('Name of the scheduled job, e.g. finance-staging-reconcile.')
param jobName string

@description('Resource ID of the Container Apps environment.')
param environmentId string

@description('Workload profile to run on.')
param workloadProfileName string = 'Consumption'

@description('Fully qualified image reference: the same image as the web app and the worker. CI replaces it with the built image.')
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

@description('Cron expression, UTC. Two firings an hour apart cover 02:00 New York in both standard and daylight time.')
param cronExpression string = '0 6,7 * * *'

@description('Longest one firing may take, in seconds: it only queues a job.')
param replicaTimeout int = 300

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
    value: 'none'
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

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: workloadProfileName
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: replicaTimeout
      // No retry: the run itself is the worker's job; a firing that failed to queue is
      // covered by the second firing in the window or by the next night (D9).
      replicaRetryLimit: 0
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registryLoginServer
          identity: identityResourceId
        }
      ]
      secrets: wireSentry ? concat(baseSecrets, sentrySecrets) : baseSecrets
    }
    template: {
      // No terminationGracePeriodSeconds: JobTemplate (2024-03-01) has no such property.
      containers: [
        {
          name: 'reconcile'
          image: image
          // Queues the run when due and exits; it makes no provider call, so there is
          // no provider boot check. `exec` makes the command PID 1.
          command: [
            '/bin/sh'
            '-c'
          ]
          args: [
            'exec python backend/manage.py reconcile --scheduled'
          ]
          resources: {
            cpu: json(cpuCores)
            memory: memory
          }
          env: wireSentry ? concat(baseEnv, sentryEnv) : baseEnv
        }
      ]
    }
  }
}

output jobId string = job.id
output jobName string = job.name
