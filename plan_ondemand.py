"""Descoberta das AZs onde um tipo de instância é oferecido."""
import config
from common import ec2_client


def region_azs(region, instance_type, cache):
    """{nome_da_az: az_id} das AZs de `region` que oferecem `instance_type`.

    Respeita config.AZS (se definido). `cache` é um dict reaproveitado entre chamadas.
    """
    key = (region, instance_type)
    if key in cache:
        return cache[key]
    client = ec2_client(region)
    offered = set()
    paginator = client.get_paginator("describe_instance_type_offerings")
    for page in paginator.paginate(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [instance_type]}]):
        offered |= {o["Location"] for o in page["InstanceTypeOfferings"]}
    zones = client.describe_availability_zones(
        Filters=[{"Name": "zone-type", "Values": ["availability-zone"]}])["AvailabilityZones"]
    allowed = (config.AZS or {}).get(region)
    azs = {z["ZoneName"]: z["ZoneId"] for z in zones
           if z["ZoneName"] in offered and (allowed is None or z["ZoneName"] in allowed)}
    if not azs:
        raise SystemExit(f"{instance_type} não é oferecido em nenhuma AZ de {region}")
    cache[key] = azs
    return azs
