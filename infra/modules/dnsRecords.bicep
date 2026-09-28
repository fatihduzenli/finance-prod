// DNS records for the web app's custom hostname, in the resource group that
// holds the public zone (monvera.io lives in finance-prod).
//
// Two records are needed, and both must exist *before* the hostname is added to
// the container app, because Azure validates ownership at that moment:
//   <label>        CNAME -> the app's default ingress FQDN
//   asuid.<label>  TXT   -> the environment's custom domain verification ID
//
// Writing DNS is a Publish action under root CLAUDE.md section 6. main.bicep
// only deploys this module when createDnsRecords is explicitly true.

@description('Name of the existing public DNS zone, e.g. monvera.io.')
param dnsZoneName string

@description('Record label within the zone, e.g. staging for staging.monvera.io. Apex records are not supported here: a zone apex cannot hold a CNAME.')
param recordLabel string

@description('Default ingress FQDN of the container app, which the CNAME points at.')
param targetFqdn string

@description('customDomainVerificationId of the Container Apps environment. An ownership proof, not a secret.')
param customDomainVerificationId string

@description('TTL in seconds. Short while the hostname is being set up; raise it once stable.')
param ttl int = 300

resource zone 'Microsoft.Network/dnsZones@2018-05-01' existing = {
  name: dnsZoneName
}

resource hostnameCname 'Microsoft.Network/dnsZones/CNAME@2018-05-01' = {
  parent: zone
  name: recordLabel
  properties: {
    TTL: ttl
    CNAMERecord: {
      cname: targetFqdn
    }
  }
}

resource ownershipTxt 'Microsoft.Network/dnsZones/TXT@2018-05-01' = {
  parent: zone
  name: 'asuid.${recordLabel}'
  properties: {
    TTL: ttl
    TXTRecords: [
      {
        value: [
          customDomainVerificationId
        ]
      }
    ]
  }
}

output cnameFqdn string = hostnameCname.properties.fqdn
output txtFqdn string = ownershipTxt.properties.fqdn
