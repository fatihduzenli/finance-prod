// Log Analytics workspace. Container Apps console and system logs land here,
// and the breaker-open and 429 alerts from Rule 3 will be built on it.

param location string
param workspaceName string

@minValue(30)
@maxValue(730)
param retentionInDays int = 30

param tags object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output workspaceId string = workspace.id
output workspaceName string = workspace.name
