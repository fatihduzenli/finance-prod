// Key Vault for this environment. Only created when createKeyVault is true:
// staging gets its own vault, production reuses the existing finance-kv
// (ADR-0002) and this module is not deployed.
//
// RBAC authorisation is on, so access is granted by role assignment
// (keyVaultAccess.bicep) rather than by access policy. Soft delete is always on:
// a deleted secret can be recovered, not silently lost. Purge protection is on
// by default; see the parameter below for why it is omitted, not set to false.

param location string
param keyVaultName string

@minValue(7)
@maxValue(90)
param softDeleteRetentionInDays int = 90

@description('Purge protection cannot be turned off once enabled, and the vault name stays reserved for the soft-delete window after a delete. Leave it on unless you expect to tear this environment down. False omits the property rather than sending false, which Azure rejects; on a vault that already has it on, false changes nothing.')
param enablePurgeProtection bool = true

param tags object = {}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    // Azure accepts only true or an absent property here: sending false fails
    // with "enablePurgeProtection cannot be set to false". null omits it.
    enablePurgeProtection: enablePurgeProtection ? true : null
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

output keyVaultId string = vault.id
output keyVaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
