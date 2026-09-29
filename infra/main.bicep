// Azure environment for the Stax invoicing app (ADR-0002, PLAN-002 Batch B).
//
// Scope: one resource group per environment. The resource group itself is
// created by an operator (see infra/README.md); this template fills it.
//
//   az deployment group create \
//     --resource-group finance-sandbox \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/staging.bicepparam
//
// Deploying this template is a Publish action under root CLAUDE.md section 6:
// it creates and modifies live cloud resources and needs explicit human
// authorisation. `--what-if` is safe and is the recommended dry run.
//
// No secret value lives in this template or in any parameter file. Application
// secrets are referenced by *name* in Key Vault and resolved at container start
// by the user-assigned managed identity.

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Environment identity
// ---------------------------------------------------------------------------

@description('Which application environment this resource group hosts.')
@allowed([
  'staging'
  'production'
])
param appEnv string

@description('Which Stax environment the app talks to. Production stays on sandbox until the live key is wired in Step 16 (docs/qa Checkpoint 2).')
@allowed([
  'sandbox'
  'live'
])
param staxEnv string = 'sandbox'

@description('Stax API base URL. Sandbox and live share this host; only the key differs (docs/CLAUDE.md Rule 6).')
param staxApiBaseUrl string = 'https://apiprod.fattlabs.com'

@description('Public base URL the app is served on; drives ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS. Leave empty to use the container app\'s own ingress host name, which avoids having to know it before the first deployment. Set it once a custom domain is bound.')
param appBaseUrl string = ''

@description('Region for every resource in this deployment.')
param location string = resourceGroup().location

@description('Region of resources that are region-independent for this workload and may already exist elsewhere: container registry, managed identity, key vault.')
param sharedResourcesLocation string = location

// ---------------------------------------------------------------------------
// Custom hostname and DNS
// ---------------------------------------------------------------------------

@description('Custom hostname for the web app, e.g. staging.monvera.io. Empty disables the whole custom-domain path and the app is reachable on its default ingress host name only.')
param customHostname string = ''

@description('Existing public DNS zone that holds the hostname above.')
param dnsZoneName string = 'monvera.io'

@description('Resource group of that DNS zone. The zone lives with the live environment.')
param dnsZoneResourceGroup string = 'finance-prod'

@description('Whether this deployment writes the CNAME and asuid TXT records. Writing DNS is a Publish action (root CLAUDE.md section 6): leave false and set the records by hand, or flip it to true only for an authorised run.')
param createDnsRecords bool = false

@description('Second pass of the custom-domain setup: attach the issued managed certificate and switch the binding to SNI-enabled TLS. Leave false on the first pass, when the certificate does not exist yet.')
param bindManagedCertificate bool = false

@description('Prefix for resource names that allow hyphens.')
param resourceNamePrefix string = 'finance-${appEnv}'

@description('Tags applied to every resource.')
param tags object = {
  workload: 'finance-invoicing'
  appEnv: appEnv
  managedBy: 'bicep'
}

// ---------------------------------------------------------------------------
// Networking
// ---------------------------------------------------------------------------

param vnetName string = '${resourceNamePrefix}-vnet'
param vnetAddressPrefix string = '10.20.0.0/16'

@description('Subnet for the Container Apps environment. /23 is the minimum Container Apps accepts.')
param infrastructureSubnetPrefix string = '10.20.0.0/23'

param postgresSubnetPrefix string = '10.20.2.0/24'
param natGatewayName string = '${resourceNamePrefix}-nat'
param natPublicIpName string = '${resourceNamePrefix}-nat-pip'

@description('Private DNS zone for the flexible server. Must end with .postgres.database.azure.com.')
param postgresPrivateDnsZoneName string = 'private.postgres.database.azure.com'

// ---------------------------------------------------------------------------
// Observability and runtime
// ---------------------------------------------------------------------------

param logAnalyticsWorkspaceName string = '${resourceNamePrefix}-logs'

@minValue(30)
@maxValue(730)
param logRetentionInDays int = 30

param containerAppsEnvironmentName string = '${resourceNamePrefix}-cae'

@description('Container registry name: 5-50 characters, letters and digits only, globally unique. Record the value; CI needs it as the ACR_NAME variable.')
param registryName string = toLower('finance${appEnv}acr${uniqueString(resourceGroup().id)}')

param registrySku string = 'Basic'

param managedIdentityName string = '${resourceNamePrefix}-id'

// ---------------------------------------------------------------------------
// Key Vault
// ---------------------------------------------------------------------------

@description('True for staging, which gets its own vault. False for production, which reuses the existing finance-kv (ADR-0002) and must not recreate it.')
param createKeyVault bool

@description('Vault that holds this environment\'s secrets.')
param keyVaultName string

@description('Resource group of that vault. Defaults to this resource group; production points at finance-prod.')
param keyVaultResourceGroupName string = resourceGroup().name

// Deterministic, so nothing has to read the vault to learn its URI.
var keyVaultUri = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/'

// ---------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------

@description('PostgreSQL server name: 3-63 lowercase letters, digits and hyphens, globally unique.')
param postgresServerName string = '${resourceNamePrefix}-pg-${uniqueString(resourceGroup().id)}'

param postgresAdministratorLogin string = 'financeadmin'
param postgresDatabaseName string = 'finance'
param postgresSkuName string = 'Standard_B1ms'
param postgresSkuTier string = 'Burstable'
param postgresVersion string = '16'
param postgresStorageSizeGB int = 32
param postgresBackupRetentionDays int = 7

@description('Administrator password. Leave unset: a fresh value is generated per deployment and written straight to Key Vault, never output. A GUID satisfies the server\'s complexity rule (lowercase letters, digits, hyphens). Re-running the deployment rotates it, so restart the web app afterwards.')
@secure()
param postgresAdministratorPassword string = newGuid()

@description('Vault secret name the application reads its DATABASE_URL from.')
param databaseUrlSecretName string = 'database-url'

@description('Vault secret name for the bare database administrator password, for operator use.')
param postgresAdministratorPasswordSecretName string = 'postgres-admin-password'

// ---------------------------------------------------------------------------
// Redis (reserved for Step 10)
// ---------------------------------------------------------------------------

@description('Off by default. Azure Cache for Redis (Microsoft.Cache/redis), which modules/redis.bicep deploys, no longer accepts new instances: Azure refuses creation and points to Azure Managed Redis instead. Nothing in the app reads Redis before Step 10, and the replacement (Azure Managed Redis, or a Redis container app in this environment) is decided at Step 10. Only set true against a subscription where an existing Microsoft.Cache/redis instance is being kept.')
param deployRedis bool = false

param redisName string = '${resourceNamePrefix}-redis'
param redisSkuName string = 'Basic'
param redisSkuFamily string = 'C'
param redisSkuCapacity int = 0

// ---------------------------------------------------------------------------
// Web container app
// ---------------------------------------------------------------------------

param webAppName string = '${resourceNamePrefix}-web'

@description('Image for the web app. The default is a placeholder: the first deployment runs before any image exists, and CI then rolls the app onto ACR_NAME.azurecr.io/finance-web:<sha>.')
param webImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

param webMinReplicas int = 1
param webMaxReplicas int = 2
param webCpuCores string = '0.5'
param webMemory string = '1Gi'

@description('Vault secret name for the Stax API key of this environment.')
param staxApiKeySecretName string

@description('Vault secret name for the inbound webhook URL token of this environment.')
param staxWebhookUrlTokenSecretName string

@description('Vault secret name for the Django secret key.')
param djangoSecretKeySecretName string = 'django-secret-key'

@description('Vault secret name for the Sentry DSN. Empty means Sentry stays off and SENTRY_DSN is not set at all; set it once the secret exists in the vault.')
param sentryDsnSecretName string = ''

// ---------------------------------------------------------------------------
// Deployment
// ---------------------------------------------------------------------------

module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    location: location
    vnetName: vnetName
    vnetAddressPrefix: vnetAddressPrefix
    infrastructureSubnetPrefix: infrastructureSubnetPrefix
    postgresSubnetPrefix: postgresSubnetPrefix
    natGatewayName: natGatewayName
    natPublicIpName: natPublicIpName
    postgresPrivateDnsZoneName: postgresPrivateDnsZoneName
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    workspaceName: logAnalyticsWorkspaceName
    retentionInDays: logRetentionInDays
    tags: tags
  }
}

module containerAppsEnvironment 'modules/containerAppsEnvironment.bicep' = {
  name: 'container-apps-environment'
  params: {
    location: location
    environmentName: containerAppsEnvironmentName
    logAnalyticsWorkspaceName: monitoring.outputs.workspaceName
    infrastructureSubnetId: network.outputs.infrastructureSubnetId
    tags: tags
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    location: sharedResourcesLocation
    registryName: registryName
    sku: registrySku
    tags: tags
  }
}

module identity 'modules/identity.bicep' = {
  name: 'identity'
  params: {
    location: sharedResourcesLocation
    identityName: managedIdentityName
    registryName: registry.outputs.registryName
    tags: tags
  }
}

module keyVault 'modules/keyVault.bicep' = if (createKeyVault) {
  name: 'key-vault'
  params: {
    location: sharedResourcesLocation
    keyVaultName: keyVaultName
    tags: tags
  }
}

module keyVaultAccess 'modules/keyVaultAccess.bicep' = {
  name: 'key-vault-access'
  scope: resourceGroup(keyVaultResourceGroupName)
  params: {
    keyVaultName: keyVaultName
    principalId: identity.outputs.principalId
  }
  dependsOn: [
    keyVault
  ]
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    serverName: postgresServerName
    administratorLogin: postgresAdministratorLogin
    administratorLoginPassword: postgresAdministratorPassword
    databaseName: postgresDatabaseName
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    postgresVersion: postgresVersion
    storageSizeGB: postgresStorageSizeGB
    backupRetentionDays: postgresBackupRetentionDays
    delegatedSubnetId: network.outputs.postgresSubnetId
    privateDnsZoneId: network.outputs.postgresPrivateDnsZoneId
    tags: tags
  }
}

module postgresSecrets 'modules/postgresSecrets.bicep' = {
  name: 'postgres-secrets'
  scope: resourceGroup(keyVaultResourceGroupName)
  params: {
    keyVaultName: keyVaultName
    databaseUrlSecretName: databaseUrlSecretName
    administratorPasswordSecretName: postgresAdministratorPasswordSecretName
    serverFullyQualifiedDomainName: postgres.outputs.fullyQualifiedDomainName
    administratorLogin: postgresAdministratorLogin
    databaseName: postgres.outputs.databaseName
    administratorLoginPassword: postgresAdministratorPassword
  }
  dependsOn: [
    keyVault
  ]
}

module redis 'modules/redis.bicep' = if (deployRedis) {
  name: 'redis'
  params: {
    location: location
    redisName: redisName
    skuName: redisSkuName
    skuFamily: redisSkuFamily
    skuCapacity: redisSkuCapacity
    tags: tags
  }
}

// The default ingress host name is only knowable after the environment exists,
// so it is resolved here rather than asked for as a parameter. It is also the
// CNAME target for a custom hostname, which is why it is derived rather than
// read back from the app: reading it from the app would make the DNS records
// depend on the app, and the app has to be created after the records.
var defaultIngressFqdn = '${webAppName}.${containerAppsEnvironment.outputs.defaultDomain}'

// Precedence: an explicit appBaseUrl wins, then the custom hostname, then the
// default ingress host name.
var effectiveAppBaseUrl = !empty(appBaseUrl) ? appBaseUrl : (!empty(customHostname) ? 'https://${customHostname}' : 'https://${defaultIngressFqdn}')

var useCustomHostname = !empty(customHostname)

// 'staging.monvera.io' in zone 'monvera.io' becomes the label 'staging'.
var customHostnameLabel = replace(customHostname, '.${dnsZoneName}', '')

var managedCertificateName = 'mc-${replace(customHostname, '.', '-')}'

// Constructed rather than taken from the certificate module's output: the web app
// must not depend on the certificate, because the certificate can only be issued
// after the hostname is bound to the web app. Pass 2 fails loudly if the
// certificate does not exist yet.
var managedCertificateId = resourceId('Microsoft.App/managedEnvironments/managedCertificates', containerAppsEnvironmentName, managedCertificateName)

module dnsRecords 'modules/dnsRecords.bicep' = if (useCustomHostname && createDnsRecords) {
  name: 'dns-records'
  scope: resourceGroup(dnsZoneResourceGroup)
  params: {
    dnsZoneName: dnsZoneName
    recordLabel: customHostnameLabel
    targetFqdn: defaultIngressFqdn
    customDomainVerificationId: containerAppsEnvironment.outputs.customDomainVerificationId
  }
}

module web 'modules/containerAppWeb.bicep' = {
  name: 'container-app-web'
  params: {
    location: location
    appName: webAppName
    environmentId: containerAppsEnvironment.outputs.environmentId
    image: webImage
    identityResourceId: identity.outputs.identityId
    registryLoginServer: registry.outputs.loginServer
    keyVaultUri: keyVaultUri
    appEnv: appEnv
    staxEnv: staxEnv
    staxApiBaseUrl: staxApiBaseUrl
    appBaseUrl: effectiveAppBaseUrl
    staxApiKeySecretName: staxApiKeySecretName
    staxWebhookUrlTokenSecretName: staxWebhookUrlTokenSecretName
    databaseUrlSecretName: databaseUrlSecretName
    djangoSecretKeySecretName: djangoSecretKeySecretName
    sentryDsnSecretName: sentryDsnSecretName
    minReplicas: webMinReplicas
    maxReplicas: webMaxReplicas
    cpuCores: webCpuCores
    memory: webMemory
    customDomainName: customHostname
    customDomainCertificateId: (useCustomHostname && bindManagedCertificate) ? managedCertificateId : ''
    tags: tags
  }
  dependsOn: [
    keyVaultAccess
    postgresSecrets
    // The ownership records have to resolve before the hostname can be bound.
    dnsRecords
  ]
}

// Requested after the web app, because issuance needs the hostname bound to an
// app in this environment. Attaching it to the binding is pass 2
// (bindManagedCertificate), see infra/README.md.
module managedCertificate 'modules/managedCertificate.bicep' = if (useCustomHostname) {
  name: 'managed-certificate'
  params: {
    location: location
    environmentName: containerAppsEnvironment.outputs.environmentName
    certificateName: managedCertificateName
    customHostname: customHostname
    tags: tags
  }
  dependsOn: [
    web
  ]
}

// The Celery worker and beat container apps belong here (Step 10, docs/CLAUDE.md
// Rule 3 and ADR-0002). They will reuse this environment, this identity, this
// image and these vault references, with a different command and, for beat,
// exactly one replica and no ingress. They are deliberately not created yet:
// nothing enqueues a task, and an idle always-on replica costs money. Their
// Redis is decided at Step 10 as well: see deployRedis above.

// ---------------------------------------------------------------------------
// Outputs. Names, IDs and public addresses only; never a secret value.
// ---------------------------------------------------------------------------

@description('Static egress IP of the Container Apps environment. This is the address to give Stax for whitelisting.')
output natEgressPublicIpAddress string = network.outputs.natPublicIpAddress

@description('Set as the GitHub repository variable ACR_NAME.')
output registryName string = registry.outputs.registryName

output registryLoginServer string = registry.outputs.loginServer

@description('Set as the GitHub repository variable WEB_APP_NAME.')
output webAppName string = web.outputs.appName

@description('Ingress host name of the web app.')
output webAppFqdn string = web.outputs.fqdn

@description('Set as the GitHub repository variable APP_BASE_URL. This is the value the app itself runs with.')
output appBaseUrl string = effectiveAppBaseUrl

@description('Custom hostname bound to the app, empty when the custom-domain path is off.')
output customHostname string = customHostname

@description('State of the custom hostname binding: none, Disabled (pass 1, no certificate yet) or SniEnabled (pass 2, TLS live).')
output customDomainBindingType string = web.outputs.customDomainBindingType

@description('Ownership proof to place in the asuid TXT record when DNS is written by hand. Not a secret.')
output customDomainVerificationId string = containerAppsEnvironment.outputs.customDomainVerificationId

@description('Default ingress host name, which is what the custom hostname CNAME must point at.')
output defaultIngressFqdn string = defaultIngressFqdn

@description('Client ID of the workload identity, for reference in app settings and diagnostics.')
output managedIdentityClientId string = identity.outputs.clientId

@description('Principal ID of the workload identity, needed when granting it access to anything else.')
output managedIdentityPrincipalId string = identity.outputs.principalId

output postgresServerFqdn string = postgres.outputs.fullyQualifiedDomainName
output postgresDatabaseName string = postgres.outputs.databaseName
@description('Empty unless deployRedis is true.')
output redisHostName string = deployRedis ? redis!.outputs.hostName : ''
output keyVaultName string = keyVaultName
output keyVaultUri string = keyVaultUri
output logAnalyticsWorkspaceName string = monitoring.outputs.workspaceName
