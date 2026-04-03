import os
import boto3
import boto3.session


def get_s3_client(logger=None):
    aws_profile = os.getenv("AWS_PROFILE")

    if aws_profile:
        if logger:
            logger.info(f"Using AWS profile: {aws_profile}")
        session = boto3.session.Session(profile_name=aws_profile)
        return session.client("s3")

    if logger:
        logger.info("Using default AWS credentials")
    return boto3.client("s3")