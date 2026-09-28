// Azure Cache for Redis, Basic C0. Reserved for Step 10: the Celery broker, the
// Stax token bucket and the circuit-breaker window (docs/CLAUDE.md Rule 3).
// Nothing reads it yet, so no redis-url secret is wired into the web app here.
//
// Access keys are never read or output by this template. When Step 10 needs
// them, an operator stores the connection URL in Key Vault by hand.

param location string
param redisName string

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param skuName string = 'Basic'

@allowed([
  'C'
  'P'
])
param skuFamily string = 'C'

@minValue(0)
@maxValue(6)
param skuCapacity int = 0

param tags object = {}

resource redis 'Microsoft.Cache/redis@2024-03-01' = {
  name: redisName
  location: location
  tags: tags
  properties: {
    sku: {
      name: skuName
      family: skuFamily
      capacity: skuCapacity
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    redisConfiguration: {
      'maxmemory-policy': 'noeviction'
    }
  }
}

output redisId string = redis.id
output redisName string = redis.name
output hostName string = redis.properties.hostName
output sslPort int = redis.properties.sslPort
