# phase1_upload_minio.py
from minio import Minio
from minio.error import S3Error
import logging
import os
from retry import retry
import urllib3

BUCKET_NAME = "weather-data"
DATA_DIR = "/mnt/data"
csv_files = [
    "noaa-daily-weather-data-2015.csv",
    "noaa-daily-weather-data-2016.csv",
    "noaa-daily-weather-data-2017.csv",
    "noaa-daily-weather-data-2018.csv",
    "noaa-daily-weather-data-2019.csv",
    "noaa-daily-weather-data-2020.csv",
]

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Cấu hình HTTP client cho MinIO
http_client = urllib3.PoolManager(
    timeout=3600.0,
    maxsize=20,
    retries=urllib3.Retry(total=15, backoff_factor=2, status_forcelist=[502, 503, 504])
)

# Kết nối MinIO
minio_client = Minio(
    "data-lake-minio-headless:9000",
    access_key="minio",
    secret_key="minio123",
    secure=False,
    http_client=http_client
)

@retry(S3Error, tries=5, delay=5, backoff=2, logger=logger)
def upload_to_minio(csv_file, local_path):
    minio_client.fput_object(BUCKET_NAME, csv_file, local_path)
    logger.info(f"Uploaded {csv_file} to MinIO")

def main():
    logger.info("Starting Phase 1: Uploading files to MinIO...")
    try:
        if not minio_client.bucket_exists(BUCKET_NAME):
            minio_client.make_bucket(BUCKET_NAME)
            logger.info(f"Created bucket: {BUCKET_NAME}")
        else:
            logger.info(f"Bucket {BUCKET_NAME} already exists")
    except S3Error as e:
        logger.error(f"Error creating bucket: {e}")
        exit(1)

    for csv_file in csv_files:
        local_path = os.path.join(DATA_DIR, csv_file)
        if os.path.exists(local_path):
            try:
                minio_client.stat_object(BUCKET_NAME, csv_file)
                logger.info(f"File {csv_file} already exists in MinIO, skipping upload")
            except S3Error:
                try:
                    upload_to_minio(csv_file, local_path)
                except Exception as e:
                    logger.error(f"Failed to upload {csv_file} after retries: {e}")
        else:
            logger.warning(f"Local file {csv_file} not found, skipping upload")
    logger.info("Phase 1 complete: All files uploaded to MinIO.")

if __name__ == "__main__":
    main()
