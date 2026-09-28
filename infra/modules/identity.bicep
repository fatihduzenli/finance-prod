// User-assigned managed identity used by every container app in this
// environment: it pulls images from the registry and reads secrets from Key
// Vault. The Key Vault role assignment lives in keyVaultAccess.bicep because
// the vault may sit in another resource group.

param location string
param identityName string

@description('Name of the container registry in this resource group to grant AcrPull on.')
param registryName string

param tags object = {}

// AcrPull
var acrPullRoleDefinitionId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, acrPullRoleDefinitionId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleDefinitionId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output identityId string = identity.id
output identityName string = identity.name
output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
