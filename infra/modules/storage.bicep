// Document storage (PLAN-010c D1): one StorageV2 account with one private blob
// container for store and lease documents.
//
// Authentication is Entra ID only. Shared key access is disabled, so no account
// key or SAS can be used against this account, and nothing in this module lists,
// outputs or passes one. The web app reaches the container with the
// user-assigned managed identity, which is granted Storage Blob Data Contributor
// on the container alone (not on the account).
//
// Deleted or overwritten blobs are kept for 30 days by versioning and soft
// delete (D9); the app itself never hard-deletes a blob.

param location string

@description('Storage account name: 3-24 lowercase letters and digits, globally unique.')
@minLength(3)
@maxLength(24)
param accountName string

@description('Name of the private blob container that holds the documents.')
param containerName string = 'documents'

@description('Principal ID of the user-assigned managed identity granted data access on the container.')
param principalId string

@description('Days a deleted blob, a deleted container and an overwritten blob version are kept.')
@minValue(1)
@maxValue(365)
param retentionDays int = 30

param tags object = {}

// Storage Blob Data Contributor: read, write and delete blobs; no key access.
var storageBlobDataContributorRoleDefinitionId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: accountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // The endpoint is reachable from the internet but accepts Entra tokens only
    // (shared keys are off). Restricting it to the Container Apps subnet, by a
    // service endpoint or a private endpoint, is a recorded hardening option for
    // the owner (PLAN-010c D1), not built here.
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: retentionDays
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: retentionDays
    }
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

resource blobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: container
  name: guid(container.id, principalId, storageBlobDataContributorRoleDefinitionId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleDefinitionId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

output accountName string = account.name

@description('Blob endpoint as Azure returns it, with a trailing slash, e.g. https://<account>.blob.core.windows.net/.')
output blobEndpoint string = account.properties.primaryEndpoints.blob

output containerName string = container.name

output roleAssignmentId string = blobDataContributor.id
