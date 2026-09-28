// Azure Container Registry. CI builds the image here with `az acr build`; the
// container app pulls it with the user-assigned managed identity, so the admin
// user stays disabled and no registry credential is ever stored.

param location string
param registryName string

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param sku string = 'Basic'

param tags object = {}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: sku
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

output registryId string = registry.id
output registryName string = registry.name
output loginServer string = registry.properties.loginServer
