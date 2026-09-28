// Grants the workload identity read access to secret values in the vault.
// Deployed at the scope of the vault's resource group, which for production is
// finance-prod (the vault is not created there, only referenced).
//
// The vault must have RBAC authorisation enabled for this to take effect; a
// vault still on access policies needs that switch flipped first.

@description('Name of the Key Vault in this resource group.')
param keyVaultName string

@description('Principal ID of the user-assigned managed identity.')
param principalId string

// Key Vault Secrets User: get and list secret values, nothing else.
var keyVaultSecretsUserRoleDefinitionId = '4633458b-17de-408a-b874-0445c86b69e6'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, principalId, keyVaultSecretsUserRoleDefinitionId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleDefinitionId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

output roleAssignmentId string = secretsUser.id
