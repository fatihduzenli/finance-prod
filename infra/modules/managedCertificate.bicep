// Free managed TLS certificate for the web app's custom hostname, issued by the
// Container Apps environment.
//
// Issuance only succeeds once (a) the CNAME and asuid TXT records resolve and
// (b) the hostname is already bound to a container app in this environment, even
// if that binding is still bindingType 'Disabled'. main.bicep therefore deploys
// this module after the web app, and the certificate is attached to the binding
// on a second pass. See infra/README.md, "Custom domains and DNS".

param location string

@description('Name of the Container Apps environment that issues the certificate.')
param environmentName string

@description('Certificate resource name. Stable, because the web app references it by constructed resource ID.')
param certificateName string

@description('Hostname the certificate is issued for, e.g. staging.monvera.io.')
param customHostname string

@description('How Azure validates control of the domain. CNAME matches the records this template creates.')
@allowed([
  'CNAME'
  'HTTP'
  'TXT'
])
param domainControlValidation string = 'CNAME'

param tags object = {}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource certificate 'Microsoft.App/managedEnvironments/managedCertificates@2024-03-01' = {
  parent: environment
  name: certificateName
  location: location
  tags: tags
  properties: {
    subjectName: customHostname
    domainControlValidation: domainControlValidation
  }
}

output certificateId string = certificate.id
output certificateName string = certificate.name
