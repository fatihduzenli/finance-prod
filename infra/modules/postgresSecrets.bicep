// Writes the generated database credentials into Key Vault, at the scope of the
// vault's resource group. Nothing here is emitted as an output and no value
// appears in any parameter file or document (root CLAUDE.md 5.2, 5.3).
//
// Re-running the deployment without pinning the password parameter generates a
// new password, updates the server and refreshes both secrets below; restart the
// web container app afterwards so it picks the new value up.

param keyVaultName string

@description('Secret name that holds the DATABASE_URL the application reads.')
param databaseUrlSecretName string

@description('Secret name that holds the bare administrator password, for operators.')
param administratorPasswordSecretName string

param serverFullyQualifiedDomainName string
param administratorLogin string
param databaseName string

@secure()
param administratorLoginPassword string

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// The password is percent-encoded so that any character is safe inside the URL.
var databaseUrl = 'postgresql://${uriComponent(administratorLogin)}:${uriComponent(administratorLoginPassword)}@${serverFullyQualifiedDomainName}:5432/${databaseName}?sslmode=require'

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: databaseUrlSecretName
  properties: {
    value: databaseUrl
    contentType: 'text/plain'
  }
}

resource administratorPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: administratorPasswordSecretName
  properties: {
    value: administratorLoginPassword
    contentType: 'text/plain'
  }
}
