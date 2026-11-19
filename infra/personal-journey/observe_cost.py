"""Read owned tagged AWS usage; never activates tags or changes a budget."""

import argparse
from datetime import UTC, date, datetime, timedelta
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config
from sova.adapters.aws_cost_observation import AWSRunCostObservation


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifacts", type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    config = Config(
        retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=20
    )
    account = session.client("sts", config=config).get_caller_identity()["Account"]
    if account != "265233844540":
        raise ValueError("Owned personal AWS account required")
    now = datetime.now(UTC)
    start = date(
        2026, 11, 19
    )  # Actual owned run creation day, not a rolling previous day.
    end = now.date() + timedelta(days=1)
    observation = AWSRunCostObservation(
        session.client("ce", region_name="us-east-1", config=config),
        run_id="b0be270e62",
        account_id=account,
    ).observe(start=start, end=end)
    observation.update(
        observed_at=datetime.now(UTC).isoformat(),
        activation_is_not_billing_delivery=True,
        billing_day_in_progress=True,
    )
    with a.artifacts.open("x") as f:
        json.dump(observation, f, indent=2)
        f.write("\n")
    print(
        json.dumps(
            {
                "available": observation["available"],
                "reason": observation["reason"],
                "complete_tco": False,
            }
        )
    )


if __name__ == "__main__":
    main()
