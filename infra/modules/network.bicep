// Virtual network for the Container Apps environment and the PostgreSQL
// flexible server, plus a NAT gateway so that all egress from the environment
// leaves through one static public IP. That IP is what Stax would whitelist
// (docs/CLAUDE.md Rule 3, ADR-0002).

@description('Azure region for all resources in this module.')
param location string

@description('Name of the virtual network.')
param vnetName string

@description('Name of the NAT gateway.')
param natGatewayName string

@description('Name of the public IP attached to the NAT gateway. This is the static egress address.')
param natPublicIpName string

@description('Address space of the virtual network.')
param vnetAddressPrefix string

@description('Subnet delegated to the Container Apps environment. Must be /23 or larger.')
param infrastructureSubnetPrefix string

@description('Subnet delegated to the PostgreSQL flexible server.')
param postgresSubnetPrefix string

@description('Private DNS zone used by the flexible server. Must end with .postgres.database.azure.com.')
param postgresPrivateDnsZoneName string

@description('Idle timeout in minutes for outbound NAT flows.')
@minValue(4)
@maxValue(120)
param natIdleTimeoutInMinutes int = 10

param tags object = {}

var infrastructureSubnetName = 'snet-container-apps'
var postgresSubnetName = 'snet-postgres'

resource natPublicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: natPublicIpName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    idleTimeoutInMinutes: 4
  }
}

resource natGateway 'Microsoft.Network/natGateways@2023-11-01' = {
  name: natGatewayName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: natIdleTimeoutInMinutes
    publicIpAddresses: [
      {
        id: natPublicIp.id
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        // Workload-profile Container Apps environments require this delegation,
        // and a NAT gateway can only be attached to such an environment.
        name: infrastructureSubnetName
        properties: {
          addressPrefix: infrastructureSubnetPrefix
          natGateway: {
            id: natGateway.id
          }
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
      {
        name: postgresSubnetName
        properties: {
          addressPrefix: postgresSubnetPrefix
          delegations: [
            {
              name: 'postgres-flexible-server'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

resource postgresPrivateDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: postgresPrivateDnsZoneName
  location: 'global'
  tags: tags
}

resource postgresPrivateDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: postgresPrivateDnsZone
  name: '${vnetName}-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

output vnetId string = vnet.id
output infrastructureSubnetId string = '${vnet.id}/subnets/${infrastructureSubnetName}'
output postgresSubnetId string = '${vnet.id}/subnets/${postgresSubnetName}'
output postgresPrivateDnsZoneId string = postgresPrivateDnsZone.id

@description('Static outbound IP of the whole Container Apps environment. Give this to Stax for whitelisting.')
output natPublicIpAddress string = natPublicIp.properties.ipAddress
