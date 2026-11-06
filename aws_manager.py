#!/usr/bin/env python3
"""
aws_manager.py - Manage EC2 instances (and peek at S3) from the command line using boto3.

Examples:
  python aws_manager.py whoami
  python aws_manager.py list
  python aws_manager.py list --state running
  python aws_manager.py start i-0abc123def4567890
  python aws_manager.py stop i-0abc123def4567890 i-0def456abc1237890
  python aws_manager.py stop --tag Env=dev            # stop every instance tagged Env=dev
  python aws_manager.py stop --tag Env=dev --dry-run  # check permissions without doing it
  python aws_manager.py reboot i-0abc123def4567890
  python aws_manager.py buckets
  python aws_manager.py --region ap-south-1 --profile personal list
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound


# ---------------------------------------------------------------- session ---

def get_session(profile=None, region=None):
    """Create a boto3 session from your ~/.aws credentials/config."""
    try:
        return boto3.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as e:
        sys.exit(f"Profile error: {e}. Run 'aws configure --profile <name>' first.")


def whoami(session):
    """Confirm which AWS account/user these credentials belong to."""
    ident = session.client("sts").get_caller_identity()
    print(f"Account : {ident['Account']}")
    print(f"ARN     : {ident['Arn']}")
    print(f"Region  : {session.region_name}")


# -------------------------------------------------------------------- EC2 ---

def _name_tag(instance):
    for tag in instance.get("Tags", []):
        if tag["Key"] == "Name":
            return tag["Value"]
    return "-"


def find_instances(ec2, ids=None, tag=None, state=None):
    """Return a list of instance dicts filtered by ids, a Key=Value tag, and/or state."""
    filters = []
    if tag:
        if "=" not in tag:
            sys.exit("--tag must look like Key=Value, e.g. Env=dev")
        key, value = tag.split("=", 1)
        filters.append({"Name": f"tag:{key}", "Values": [value]})
    if state:
        filters.append({"Name": "instance-state-name", "Values": [state]})

    kwargs = {"Filters": filters}
    if ids:
        kwargs["InstanceIds"] = ids

    instances = []
    paginator = ec2.get_paginator("describe_instances")  # handles accounts with many instances
    for page in paginator.paginate(**kwargs):
        for reservation in page["Reservations"]:
            instances.extend(reservation["Instances"])
    return instances


def list_instances(ec2, state=None, tag=None):
    instances = find_instances(ec2, tag=tag, state=state)
    if not instances:
        print("No instances found in this region.")
        return
    print(f"{'INSTANCE ID':<21} {'NAME':<25} {'STATE':<11} {'TYPE':<12} {'PUBLIC IP':<16} {'PRIVATE IP'}")
    print("-" * 100)
    for i in instances:
        print(f"{i['InstanceId']:<21} {_name_tag(i)[:24]:<25} {i['State']['Name']:<11} "
              f"{i['InstanceType']:<12} {i.get('PublicIpAddress', '-'):<16} {i.get('PrivateIpAddress', '-')}")


def _resolve_targets(ec2, ids, tag):
    if not ids and not tag:
        sys.exit("Give one or more instance IDs, or --tag Key=Value.")
    targets = [i["InstanceId"] for i in find_instances(ec2, ids=ids or None, tag=tag)]
    if not targets:
        sys.exit("No matching instances found.")
    return targets


def change_state(ec2, action, ids, tag, dry_run=False, wait=True):
    """action is 'start', 'stop' or 'reboot'."""
    targets = _resolve_targets(ec2, ids, tag)
    print(f"{action.upper()} -> {', '.join(targets)}")

    call = {"start": ec2.start_instances,
            "stop": ec2.stop_instances,
            "reboot": ec2.reboot_instances}[action]
    try:
        call(InstanceIds=targets, DryRun=dry_run)
    except ClientError as e:
        if e.response["Error"]["Code"] == "DryRunOperation":
            print("Dry run OK: you have permission. Re-run without --dry-run to do it for real.")
            return
        raise

    if action == "reboot" or not wait:
        print("Request sent.")
        return

    waiter_name = "instance_running" if action == "start" else "instance_stopped"
    print(f"Waiting for instances to reach '{waiter_name.split('_')[1]}' ...")
    ec2.get_waiter(waiter_name).wait(InstanceIds=targets)
    print("Done.")
    list_instances_by_id(ec2, targets)


def list_instances_by_id(ec2, ids):
    for i in find_instances(ec2, ids=ids):
        print(f"  {i['InstanceId']}  {_name_tag(i):<25} {i['State']['Name']}  {i.get('PublicIpAddress', '')}")


# --------------------------------------------------------------------- S3 ---

def list_buckets(session):
    buckets = session.client("s3").list_buckets().get("Buckets", [])
    if not buckets:
        print("No S3 buckets.")
    for b in buckets:
        print(f"{b['Name']:<50} created {b['CreationDate']:%Y-%m-%d}")


# -------------------------------------------------------------------- CLI ---

def build_parser():
    p = argparse.ArgumentParser(description="Simple AWS account manager (EC2 + S3).")
    p.add_argument("--profile", help="AWS CLI profile name (default: 'default')")
    p.add_argument("--region", help="AWS region, e.g. ap-south-1 (default: from your AWS config)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="Show which account/user you're connected as")

    lp = sub.add_parser("list", help="List EC2 instances")
    lp.add_argument("--state", choices=["pending", "running", "stopping", "stopped", "terminated"])
    lp.add_argument("--tag", help="Filter by tag, Key=Value")

    for action in ("start", "stop", "reboot"):
        sp = sub.add_parser(action, help=f"{action.capitalize()} EC2 instances")
        sp.add_argument("ids", nargs="*", help="Instance IDs (i-...)")
        sp.add_argument("--tag", help="Target all instances with this tag, Key=Value")
        sp.add_argument("--dry-run", action="store_true", help="Check permissions only")
        sp.add_argument("--no-wait", action="store_true", help="Don't wait for the state change")

    sub.add_parser("buckets", help="List S3 buckets")
    return p


def main():
    args = build_parser().parse_args()
    session = get_session(args.profile, args.region)
    if not session.region_name and args.command not in ("whoami", "buckets"):
        sys.exit("No region set. Use --region ap-south-1 or set one with 'aws configure'.")

    try:
        if args.command == "whoami":
            whoami(session)
        elif args.command == "buckets":
            list_buckets(session)
        else:
            ec2 = session.client("ec2")
            if args.command == "list":
                list_instances(ec2, state=args.state, tag=args.tag)
            else:
                change_state(ec2, args.command, args.ids, args.tag,
                             dry_run=args.dry_run, wait=not args.no_wait)
    except NoCredentialsError:
        sys.exit("No AWS credentials found. Run 'aws configure' first.")
    except ClientError as e:
        err = e.response["Error"]
        sys.exit(f"AWS error [{err['Code']}]: {err['Message']}")


if __name__ == "__main__":
    main()
