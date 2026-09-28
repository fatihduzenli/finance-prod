// Container Apps environment on the delegated subnet, with workload profiles so
// that the NAT gateway from the network module governs egress. Logs go to the
// Log Analytics workspace created in the same resource group.
//
// The workspace shared key is read here with listKeys() and passed straight to
// the environment. It is never emitted as an output (root CLAUDE.md 5.2).

param location string
param environmentName string

@description('Name of the Log Analytics workspace in this resource group.')
param logAnalyticsWorkspaceName string

@description('Resource ID of the subnet delegated to Microsoft.App/environments.')
param infrastructureSubnetId string

param tags object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      // The web app needs public ingress; egress still leaves via the NAT
      // gateway attached to this subnet.
      internal: false
      infrastructureSubnetId: infrastructureSubnetId
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

output environmentId string = environment.id
output environmentName string = environment.name
output defaultDomain string = environment.properties.defaultDomain
output staticIp string = environment.properties.staticIp

@description('Ownership proof for custom hostnames in this environment; goes into the asuid TXT record. Not a secret.')
output customDomainVerificationId string = environment.properties.customDomainConfiguration.customDomainVerificationId
