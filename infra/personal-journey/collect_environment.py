"""Read current run-owned AWS metadata, public prices and AI route locations.

No inference invocation, policy changes, prompt/log content or secret values.
Unknown costs and unknown provider data phases stay unknown, never zero/safe.
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


RUN = "b0be270e62"
ACCOUNT = "265233844540"
INSTANCE = "i-04946f0e0c0548c3b"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise ValueError("Preserve prior observation; use a new output filename")
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    config = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1, "mode": "standard"})
    if session.client("sts", config=config).get_caller_identity()["Account"] != ACCOUNT:
        raise ValueError("Personal account only")
    ec2 = session.client("ec2", config=config)
    item = ec2.describe_instances(InstanceIds=[INSTANCE])["Reservations"][0]["Instances"][0]
    if {t["Key"]: t["Value"] for t in item["Tags"]}.get("RunId") != RUN:
        raise ValueError("Source ownership changed")
    groups = ec2.describe_security_groups(GroupIds=[g["GroupId"] for g in item["SecurityGroups"]])["SecurityGroups"]
    report = {"observed_at": datetime.now(UTC).isoformat(), "run_id": RUN,
        "source": {"instance_id": INSTANCE, "state": item["State"]["Name"], "architecture": item["Architecture"],
            "instance_type": item["InstanceType"], "imds": item["MetadataOptions"],
            "network_inbound_rules": sum(len(g["IpPermissions"]) for g in groups)},
        "cost": {"currency": "USD", "compute_hourly_list_rate": None,
            "actual_billed_cost": None, "storage": None, "network": None, "backup": None,
            "operations": None, "shared": None, "local_vm_tco": None,
            "cheapest_target_decision": "BLOCKED_INCOMPLETE_COMPARABLE_COSTS"},
        "ai": {"invocations": 0, "profiles": [], "all_data_phases_authenticated": False,
            "residency_admission": "BLOCKED_UNQUALIFIED_PROVIDER_FACTS"},
        "operational_qualification": False, "migration_complete": False}
    try:
        pricing = session.client("pricing", region_name="us-east-1", config=config)
        response = pricing.get_products(ServiceCode="AmazonEC2", MaxResults=20, Filters=[
            {"Type": "TERM_MATCH", "Field": field, "Value": value} for field, value in (
                ("regionCode", "ap-northeast-2"), ("instanceType", "t4g.small"), ("operatingSystem", "Linux"),
                ("tenancy", "Shared"), ("preInstalledSw", "NA"), ("capacitystatus", "Used"))])
        rates = []
        for raw in response["PriceList"]:
            product = json.loads(raw)
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for rate in term["priceDimensions"].values():
                    if rate["unit"] == "Hrs" and "USD" in rate["pricePerUnit"]:
                        rates.append({"sku": product["product"]["sku"], "usd": rate["pricePerUnit"]["USD"],
                            "unit": "Hrs", "effective_at": term["effectiveDate"], "source": "AWS Price List GetProducts"})
        if len(rates) == 1 and not response.get("NextToken"):
            report["cost"]["compute_hourly_list_rate"] = rates[0]
        else:
            report["cost"]["price_status"] = "UNDECIDED_MULTIPLE_OR_PAGINATED_RATES"
    except ClientError as exc:
        report["cost"]["price_status"] = exc.response["Error"]["Code"]
    bedrock = session.client("bedrock", config=config)
    try:
        logging = bedrock.get_model_invocation_logging_configuration()
        # Presence and destinations only, never log payloads or content.
        settings = logging.get("loggingConfig", {})
        report["ai"]["account_logging_configuration_present"] = bool(settings)
        report["ai"]["account_logging_destinations"] = [key for key in ("cloudWatchConfig", "s3Config") if key in settings]
        report["ai"]["provider_retention"] = "UNKNOWN_NOT_PROVEN_BY_ACCOUNT_LOGGING_SETTINGS"
        profiles = bedrock.list_inference_profiles(typeEquals="SYSTEM_DEFINED", maxResults=100)
        candidates = [row for row in profiles.get("inferenceProfileSummaries", [])
                      if "nova-lite" in row["inferenceProfileId"]]
        for row in candidates[:2]:
            actual = bedrock.get_inference_profile(inferenceProfileIdentifier=row["inferenceProfileArn"])
            regions = sorted({m["modelArn"].split(":")[3] for m in actual["models"]})
            report["ai"]["profiles"].append({"id": actual["inferenceProfileId"], "status": actual["status"],
                "model_regions": regions, "seoul_api_is_processing_location_proof": False})
        report["ai"]["profile_listing_complete"] = not bool(profiles.get("nextToken"))
    except ClientError as exc:
        report["ai"]["collection_status"] = exc.response["Error"]["Code"]
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
