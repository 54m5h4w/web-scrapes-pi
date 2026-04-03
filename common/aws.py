import os
import boto3
import boto3.session

def get_s3_client(logger=None):
    profile = os.getenv("AWS_PROFILE", "raspberry-pi-scraper")
    session = boto3.Session(profile_name=profile)
    return session.client("s3")