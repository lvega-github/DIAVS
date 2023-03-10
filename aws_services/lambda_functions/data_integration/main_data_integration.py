import boto3
import json
import re
import os
import io
from io import BytesIO
from datetime import datetime
import tempfile

# Packages from layers
import cchardet
import pandas as pd

from aux_data_integration import *

REGION = os.getenv("REGION")
INPUT_RAW_BUCKET = os.getenv("INPUT_RAW_BUCKET")
RAW_ZONE_BUCKET = os.getenv("RAW_ZONE_BUCKET")
LANDING_ZONE_BUCKET = os.getenv("LANDING_ZONE_BUCKET")
STAGING_ZONE_BUCKET = os.getenv("STAGING_ZONE_BUCKET")
ERROR_ZONE_BUCKET = os.getenv("ERROR_ZONE_BUCKET")
s3_client = boto3.client("s3")
sns = boto3.client("sns")
dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table("inventory_per_district")


def get_topic_arn(topic_name):
    """
    This function retrieves the ARN of a topic with the specified name.
    It retrieves all the topics available in the SNS service and looks
    for the topic with the specified name.
    """
    response = sns.list_topics()
    topics = response["Topics"]
    next_token = response.get("NextToken", None)
    while next_token:
        response = sns.list_topics(NextToken=next_token)
        topics += response["Topics"]
        next_token = response.get("NextToken", None)
    for topic in topics:
        if topic["TopicArn"].split(":")[-1] == topic_name:
            return topic["TopicArn"]
    return None


def write_df_to_s3_parquet(df, output_bucket_name, output_prefix):
    """
    This function writes a dataframe to S3 in parquet format.
    The dataframe is written to a temporary file in the local filesystem,
    and then uploaded to S3.

    Parameters:
        df (pandas.DataFrame): The dataframe to be written to S3.
        output_bucket_name (str): The name of the S3 bucket to which the dataframe will be written.
        output_prefix (str): The name of the output file in S3.

    Returns:
        None
    """

    # Create a temporary file
    with tempfile.NamedTemporaryFile() as temp:

        # Write the dataframe to the temporary file in parquet format
        df.to_parquet(temp.name, index=False, compression='snappy')

        # Move the file pointer to the beginning of the file
        temp.seek(0)

        # Upload the file to S3
        output_prefix = output_prefix + ".parquet"
        s3_client.upload_file(temp.name, output_bucket_name, output_prefix)


def get_validation_rules(prefix):

    document_key = "/".join(re.split("/", prefix)[:-1])
    file_name = prefix.rsplit("/", 1)[-1]

    response = table.get_item(Key={"document_key": document_key})
    district_documents = response.get("Item")

    valid_file = False
    for _, district_data in district_documents["files"].items():
        if re.match(district_data["file_name_regex"], file_name):
            district_key = district_data["district_key"]
            output_base_file_name = district_data["output_base_file_name"]
            valid_file = True
    if valid_file:
        response = table.get_item(Key={"document_key": district_key})
        district_rules = response.get("Item")
        return True, district_rules, output_base_file_name, file_name, document_key
    return False, _, _, _, _


def get_file_extract(input_bucket_name, prefix, file_name, district_rules):
    extension_status = False
    encoding_status = False
    file_extension = district_rules["validation_rules"].get(
        "file_extension")
    if "file_extension" in district_rules["validation_rules"]:
        extension_status = validate_file_extension(
            file_name, file_extension
        )

    if extension_status:
        bytes_min_range = 0
        bytes_delta = 2500
        bytes_max_range = bytes_delta

        # Create a bytes buffer to hold the first two lines of the file
        buffer = io.BytesIO()

        # Download the first two lines of the file into the buffer
        s3_client.download_fileobj(
            Bucket=input_bucket_name, Key=prefix, Fileobj=buffer, Config={"Range": f"bytes={bytes_min_range}-{bytes_max_range}"})
        file_bytes = buffer.getvalue()
        if "encoding" in district_rules["validation_rules"] and extension_status:
            encoding = district_rules["validation_rules"].get("encoding")
            encoding_status = validate_file_encoding(
                file_bytes, encoding)

    if encoding_status:
        record_delimiter = get_record_delimiter(file_extension, encoding)
        max_iterations = 10
        i = 0
        while i < max_iterations:
            buffer.seek(0)
            file_content = buffer.read().decode(encoding)
            if len(file_content.split(record_delimiter)) < 3:
                i += 1
                # Append the next bytes
                bytes_min_range += bytes_delta
                bytes_max_range += bytes_delta
                buffer.seek(0, 2)  # set the pointer to the end of the buffer
                s3_client.download_fileobj(
                    Bucket=input_bucket_name, Key=prefix, Fileobj=buffer, Config={"Range": f"bytes={bytes_min_range}-{bytes_max_range}"})
            else:
                break
        if not file_content:
            raise Exception("The file is empty")
        if buffer.tell() >= 1000000:
            raise Exception("The buffer has reached the maximum size")

        file_extract = file_content.split(record_delimiter)[:2]
        return file_extract
    else:
        return None


def create_dataframe(input_bucket_name, prefix, district_rules):
    columns_details = district_rules["validation_rules"].get("columns_details")
    encoding = district_rules["validation_rules"].get("encoding")
    delimiter = district_rules["validation_rules"].get("delimiter")

    dtypes = {}
    for col in columns_details:
        column_name = col["header"]
        column_type = col["data_type"]
        if column_type == "date":
            dtypes[column_name] = "string"
        else:
            dtypes[column_name] = column_type

    obj = s3_client.get_object(Bucket=input_bucket_name, Key=prefix)
    file_content = obj["Body"].read().decode(encoding)

    df = pd.read_csv(io.BytesIO(bytes(file_content, encoding)),
                     dtype=dtypes, delimiter=delimiter)

    for col in columns_details:
        if col["data_type"] == "date" and "date_format" in col:
            df[col["header"]] = pd.to_datetime(
                df[col["header"]], format=col["date_format"])

    return df


def lambda_handler(event, context):

    input_bucket_name = event["Records"][0]["s3"]["bucket"]["name"]
    prefix = event["Records"][0]["s3"]["object"]["key"]
    if INPUT_RAW_BUCKET == input_bucket_name:
        file_exist = False
        file_extract = None
        file_properties = False

        file_exist, district_rules, output_base_file_name, file_name, document_key = get_validation_rules(
            prefix)

        if file_exist:
            file_extract = get_file_extract(
                input_bucket_name, prefix, file_name, district_rules)

        if file_extract != None:
            print(file_extract)
            if "delimiter" in district_rules["validation_rules"]:
                delimiter = district_rules["validation_rules"].get("delimiter")
                file_extract = normalize_headers(
                    file_extract, delimiter)
                if "columns_count" in district_rules["validation_rules"]:
                    number_of_columns_status = validate_file_number_of_columns(
                        file_extract,
                        district_rules["validation_rules"].get(
                            "columns_count"),
                        delimiter,
                    )
                if "columns_details" in district_rules["validation_rules"]:
                    columns_names_status = validate_file_columns_names(
                        file_extract,
                        district_rules["validation_rules"].get(
                            "columns_details"),
                        delimiter,
                    )
                if number_of_columns_status and columns_names_status:
                    file_properties = True

        if file_properties:
            df = create_dataframe(input_bucket_name, prefix, district_rules)

            if "date_details" in district_rules["validation_rules"]:
                date_details = district_rules["validation_rules"].get(
                    "date_details")

                df, output_file_name = add_date_columns(
                    df, date_details, file_name, output_base_file_name)
            else:
                output_file_name = output_base_file_name

            output_bucket_name = re.sub(
                INPUT_RAW_BUCKET, STAGING_ZONE_BUCKET, input_bucket_name)
            output_prefix = prefix.rsplit(
                "/", 1)[0] + "/" + output_file_name

            print(output_file_name)

            write_df_to_s3_parquet(
                df, output_bucket_name, output_prefix)
