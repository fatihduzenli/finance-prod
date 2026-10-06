// The `daily` scheduled job: the collections tick (docs/qa/PLAN-011b D8, D9).
//
// A clone of the reconcile job (containerAppJobReconcile.bicep) with its own command and
// cron. A Container Apps job with a Schedule trigger, once a day at 13:00 UTC (09:00 New
// York in daylight time, 08:00 in standard time). It runs `collections_tick`, which only
// queues: the collection e-mails of the ladder, the morning e-mails of the calendar and
// the promise reminders become `message_send` and `calendar_email` jobs in the database;
// the worker renders and sends them. The job never calls Stax (STAX_CALLER=none, no
// provider boot check) and sends no e-mail itself. One replica, no retry: the tick is
// idempotent per charge, step and day, so a failed firing is redone by the next day's.
// Same image, identity and vault references as the reconcile job, plus the mail settings
// and, only while mail is on, the Gmail service-account key by vault reference: the
// environment guard checks the mail settings in every process. No document setting.
//
// No secret value appears in this template, in a parameter file or in an output;
// only secret *names* do (root CLAUDE.md 5.3).

param location string
@description('Name of the scheduled job, e.g. finance-staging-daily.')
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

@description('Cron expression, UTC: once a day at 13:00 (09:00 New York in daylight time).')
param cronExpression string = '0 13 * * *'

@description('Longest one firing may take, in seconds: it only queues jobs.')
param replicaTimeout int = 300

@description('MAIL_ENABLED (PLAN-011b D3), as on the web app and the worker. While false the service-account key is not referenced at all.')
param mailEnabled bool = false

@description('GMAIL_SENDER: the company mailbox. An address, not a secret.')
param gmailSender string = ''

@description('MAIL_CATCH_ALL: the staff address that receives every e-mail outside production. An address, not a secret.')
param mailCatchAll string = ''

@description('COMPANY_NAME: the {company} merge field of the e-mails.')
param companyName string = 'Global Retail Groups'

@description('Vault secret name for the Gmail service-account key (JSON). Referenced only when mailEnabled is true.')
param gmailServiceAccountSecretName string = 'gmail-service-account-key'

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
    name: 'MAIL_ENABLED'
    value: mailEnabled ? 'true' : 'false'
  }
  {
    name: 'GMAIL_SENDER'
    value: gmailSender
  }
  {
    name: 'MAIL_CATCH_ALL'
    value: mailCatchAll
  }
  {
    name: 'COMPANY_NAME'
    value: companyName
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

// PLAN-011b D3, D9: the Gmail service-account key, by vault reference, only while mail
// is on (the env guard requires it then, and nothing reads it otherwise).
var mailSecrets = [
  {
    name: 'gmail-service-account-json'
    keyVaultUrl: '${keyVaultUri}secrets/${gmailServiceAccountSecretName}'
    identity: identityResourceId
  }
]

var mailSecretEnv = [
  {
    name: 'GMAIL_SERVICE_ACCOUNT_JSON'
    secretRef: 'gmail-service-account-json'
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
      // No retry: the sending is the worker's job; a firing that failed to queue is
      // covered by the next day's, which queues every step still due.
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
      secrets: concat(baseSecrets, wireSentry ? sentrySecrets : [], mailEnabled ? mailSecrets : [])
    }
    template: {
      // No terminationGracePeriodSeconds: JobTemplate (2024-03-01) has no such property.
      containers: [
        {
          name: 'daily'
          image: image
          // Queues the day's e-mails and exits; it makes no provider call, so there is no
          // provider boot check. `exec` makes the command PID 1.
          command: [
            '/bin/sh'
            '-c'
          ]
          args: [
            'exec python backend/manage.py collections_tick'
          ]
          resources: {
            cpu: json(cpuCores)
            memory: memory
          }
          env: concat(baseEnv, wireSentry ? sentryEnv : [], mailEnabled ? mailSecretEnv : [])
        }
      ]
    }
  }
}

output jobId string = job.id
output jobName string = job.name
