// The `web` container app: Django plus the admin, served by gunicorn on 8000.
//
// Every secret reaches the process as a Key Vault reference resolved by the
// user-assigned managed identity at container start (ADR-0002, root CLAUDE.md
// 5.3). No secret value appears in this template, in a parameter file or in an
// output; only secret *names* do.

param location string
param appName string

@description('Resource ID of the Container Apps environment.')
param environmentId string

@description('Workload profile to run on.')
param workloadProfileName string = 'Consumption'

@description('Fully qualified image reference. On the first deployment this is a placeholder; CI replaces it with the built image.')
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

@description('STAX_CALLER: none, or worker for the single process allowed to call Stax.')
@allowed([
  'none'
  'worker'
])
param staxCaller string = 'none'

@description('STAX_EXPECTED_MERCHANT_ID: the merchant the Stax key must belong to. An identifier, not a secret. Empty means not checked.')
param staxExpectedMerchantId string = ''

@description('STAX_MAX_TXN_AMOUNT: the ACH per-transaction limit confirmed by Stax (PLAN-007c). Not a secret. Empty means unknown: no hard block.')
param staxMaxTxnAmount string = ''

@description('Public base URL of this app. Drives ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS in Django.')
param appBaseUrl string

@description('DOCUMENTS_STORAGE_ACCOUNT_URL: blob endpoint of the document storage account (PLAN-010c D1). A plain URL, not a secret.')
param documentsStorageAccountUrl string

@description('DOCUMENTS_CONTAINER: name of the private blob container for documents. Not a secret.')
param documentsContainer string

@description('AZURE_CLIENT_ID: client ID of the user-assigned managed identity the app authenticates to Blob Storage with. An identifier, not a secret.')
param azureClientId string

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

@description('MAIL_ENABLED (PLAN-011b D3): true sends the collection e-mails through the Gmail API. While false the service-account key is not referenced at all, so the deployment works before the owner has stored it in the vault.')
param mailEnabled bool = false

@description('GMAIL_SENDER: the company mailbox the e-mails are sent as. An address, not a secret.')
param gmailSender string = ''

@description('MAIL_CATCH_ALL: when set, every e-mail goes to this staff address instead (required outside production while mail is on). An address, not a secret; passed on the command line at deploy time.')
param mailCatchAll string = ''

@description('COMPANY_NAME: the {company} merge field of the e-mails.')
param companyName string = 'Global Retail Groups'

@description('Vault secret name for the Gmail service-account key (JSON). Referenced only when mailEnabled is true.')
param gmailServiceAccountSecretName string = 'gmail-service-account-key'

@description('Custom hostname to bind, e.g. staging.monvera.io. Empty means the app is reachable on its default ingress host name only.')
param customDomainName string = ''

@description('Resource ID of the managed certificate for that hostname. While it is empty the hostname is bound with bindingType Disabled, which is the first pass of the documented two-pass setup (infra/README.md).')
param customDomainCertificateId string = ''

@minValue(1)
param minReplicas int = 1

@minValue(1)
param maxReplicas int = 2

@description('CPU cores as a string, converted with json(). Must pair with memory per the Container Apps allowed combinations.')
param cpuCores string = '0.5'

param memory string = '1Gi'

@description('Concurrent requests per replica before scaling out.')
param httpConcurrentRequests int = 50

param tags object = {}

// Container Apps probes address the replica directly, so they must send the
// public host name for Django's ALLOWED_HOSTS check to pass.
var appBaseHost = split(replace(replace(appBaseUrl, 'https://', ''), 'http://', ''), '/')[0]

var wireSentry = !empty(sentryDsnSecretName)

// Pass 1 binds the hostname without a certificate, which is what lets the
// environment issue one. Pass 2 supplies the certificate ID and switches the
// binding to SNI-enabled TLS.
var customDomainWithoutCertificate = [
  {
    name: customDomainName
    bindingType: 'Disabled'
  }
]

var customDomainWithCertificate = [
  {
    name: customDomainName
    bindingType: 'SniEnabled'
    certificateId: customDomainCertificateId
  }
]

var customDomains = empty(customDomainName) ? [] : (empty(customDomainCertificateId) ? customDomainWithoutCertificate : customDomainWithCertificate)

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
    value: staxCaller
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
    name: 'DOCUMENTS_STORAGE_ACCOUNT_URL'
    value: documentsStorageAccountUrl
  }
  {
    name: 'DOCUMENTS_CONTAINER'
    value: documentsContainer
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: azureClientId
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

resource web 'Microsoft.App/containerApps@2024-03-01' = {
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
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        customDomains: customDomains
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
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
      containers: [
        {
          name: 'web'
          image: image
          resources: {
            cpu: json(cpuCores)
            memory: memory
          }
          env: concat(baseEnv, wireSentry ? sentryEnv : [], mailEnabled ? mailSecretEnv : [])
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
                httpHeaders: [
                  {
                    name: 'Host'
                    value: appBaseHost
                  }
                ]
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              failureThreshold: 6
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
                httpHeaders: [
                  {
                    name: 'Host'
                    value: appBaseHost
                  }
                ]
              }
              initialDelaySeconds: 30
              periodSeconds: 30
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        // Never scales to zero: the app must answer Stax webhooks immediately.
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: string(httpConcurrentRequests)
              }
            }
          }
        ]
      }
    }
  }
}

output appId string = web.id
output appName string = web.name
output fqdn string = web.properties.configuration.ingress.fqdn
output customDomainBindingType string = empty(customDomainName) ? 'none' : (empty(customDomainCertificateId) ? 'Disabled' : 'SniEnabled')
