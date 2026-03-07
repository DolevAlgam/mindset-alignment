"""Provision AWS infrastructure for call orchestration.

Creates/updates: S3 bucket, IAM roles/policies, EventBridge schedule group +
recurring schedules, Lambda env vars + timeout.

Required env vars: RETELL_API_KEY, FROM_NUMBER, TO_NUMBER, AGENT_ID
"""

import json
import os
import secrets
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
LAMBDA_NAME = "retell-slack-bridge"
LAMBDA_ROLE_NAME = "retell-slack-bridge-role"
SCHEDULER_ROLE_NAME = "retell-scheduler-role"
SCHEDULE_GROUP = "retell-calls"
TIMEZONE = "America/New_York"


def main():
    sts = boto3.client("sts", region_name=REGION)
    account_id = sts.get_caller_identity()["Account"]

    lambda_arn = f"arn:aws:lambda:{REGION}:{account_id}:function:{LAMBDA_NAME}"
    s3_bucket = f"retell-call-state-{account_id}"
    scheduler_role_arn = f"arn:aws:iam::{account_id}:role/{SCHEDULER_ROLE_NAME}"

    s3 = boto3.client("s3", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    scheduler = boto3.client("scheduler", region_name=REGION)
    lambda_client = boto3.client("lambda", region_name=REGION)

    print(f"=== Provisioning (account {account_id}) ===\n")

    # S3 bucket
    try:
        s3.create_bucket(Bucket=s3_bucket)
        print(f"Created S3 bucket: {s3_bucket}")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"S3 bucket already exists: {s3_bucket}")
        else:
            raise

    # Scheduler execution role
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "scheduler.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })
    try:
        iam.create_role(
            RoleName=SCHEDULER_ROLE_NAME,
            AssumeRolePolicyDocument=trust_policy,
            Description="Allows EventBridge Scheduler to invoke Lambda",
        )
        print(f"Created IAM role: {SCHEDULER_ROLE_NAME}")
        time.sleep(10)
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            print(f"IAM role already exists: {SCHEDULER_ROLE_NAME}")
        else:
            raise

    iam.put_role_policy(
        RoleName=SCHEDULER_ROLE_NAME,
        PolicyName="invoke-lambda",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn}],
        }),
    )
    print(f"  Set invoke-lambda policy on {SCHEDULER_ROLE_NAME}")

    # Lambda role policy
    iam.put_role_policy(
        RoleName=LAMBDA_ROLE_NAME,
        PolicyName="retell-call-orchestration",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "S3State", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{s3_bucket}/*"},
                {"Sid": "Scheduler", "Effect": "Allow", "Action": ["scheduler:CreateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule"], "Resource": f"arn:aws:scheduler:{REGION}:{account_id}:schedule/{SCHEDULE_GROUP}/*"},
                {"Sid": "PassSchedulerRole", "Effect": "Allow", "Action": "iam:PassRole", "Resource": scheduler_role_arn},
            ],
        }),
    )
    print(f"Set retell-call-orchestration policy on {LAMBDA_ROLE_NAME}")

    # Schedule group
    try:
        scheduler.create_schedule_group(Name=SCHEDULE_GROUP)
        print(f"Created schedule group: {SCHEDULE_GROUP}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConflictException":
            print(f"Schedule group already exists: {SCHEDULE_GROUP}")
        else:
            raise

    # Recurring schedules
    for name, cron_expr, action in [
        ("daily-fallback", "cron(0 12 * * ? *)", "daily_fallback"),
        ("cutoff-log", "cron(0 20 * * ? *)", "cutoff_log"),
    ]:
        target = {"Arn": lambda_arn, "RoleArn": scheduler_role_arn, "Input": json.dumps({"action": action})}
        kwargs = dict(Name=name, GroupName=SCHEDULE_GROUP, ScheduleExpression=cron_expr,
                      ScheduleExpressionTimezone=TIMEZONE, Target=target, FlexibleTimeWindow={"Mode": "OFF"}, State="ENABLED")
        try:
            scheduler.create_schedule(**kwargs)
            print(f"Created schedule: {name}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConflictException":
                scheduler.update_schedule(**kwargs)
                print(f"Updated schedule: {name}")
            else:
                raise

    # Lambda config
    current = lambda_client.get_function_configuration(FunctionName=LAMBDA_NAME)
    env_vars = current.get("Environment", {}).get("Variables", {})

    if "RETELL_API_KEY" not in env_vars:
        env_vars["RETELL_API_KEY"] = os.environ["RETELL_API_KEY"]
    new_trigger_key = "TRIGGER_API_KEY" not in env_vars
    if new_trigger_key:
        env_vars["TRIGGER_API_KEY"] = secrets.token_urlsafe(32)

    env_vars.update({
        "S3_BUCKET": s3_bucket,
        "FROM_NUMBER": os.environ["FROM_NUMBER"],
        "TO_NUMBER": os.environ["TO_NUMBER"],
        "AGENT_ID": os.environ["AGENT_ID"],
        "TIMEZONE": TIMEZONE,
        "FALLBACK_HOUR": "12",
        "CUTOFF_HOUR": "20",
        "SCHEDULER_ROLE_ARN": scheduler_role_arn,
        "LAMBDA_ARN": lambda_arn,
    })

    lambda_client.update_function_configuration(
        FunctionName=LAMBDA_NAME, Timeout=30, Environment={"Variables": env_vars},
    )
    print(f"Set Lambda config: timeout=30s, env vars set")
    if new_trigger_key:
        print(f"  NEW TRIGGER_API_KEY = {env_vars['TRIGGER_API_KEY']}")
    else:
        print(f"  TRIGGER_API_KEY unchanged")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
