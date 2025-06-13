import argparse
import logging
import csv
import json
import time
import os
from minio import Minio
from minio.error import S3Error
from kafka import KafkaProducer, KafkaAdminClient
from kafka.admin import NewTopic
from kafka.errors import NoBrokersAvailable, KafkaError
from retry import retry
from io import StringIO
import urllib3
from http.client import IncompleteRead

BUCKET_NAME = "weather-data"
DATA_DIR = "/data"
TOPIC = "weather-topic"
BATCH_SIZE = 5000  # Tăng batch size để cải thiện hiệu suất
BATCH_DELAY = 0.001
FLUSH_INTERVAL = 5000  # Flush ít thường xuyên hơn
MAX_RESUME_ATTEMPTS = 5
CHUNK_SIZE = 10 * 1024 * 1024  # 10MB
STATE_FILE = "/tmp/processor_state.json"
MAX_INVALID_ROWS = 100  # Thoát nếu gặp quá nhiều dòng lỗi liên tiếp

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
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

# Hàm lưu trạng thái
def save_state(csv_file, bytes_read, record_count, chunk_index):
    state = {
        "csv_file": csv_file,
        "bytes_read": bytes_read,
        "record_count": record_count,
        "chunk_index": chunk_index
    }
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)
    logger.info(f"Saved state for {csv_file}: bytes_read={bytes_read}, record_count={record_count}, chunk_index={chunk_index}")

# Hàm đọc trạng thái
def load_state(csv_file):
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
        if state["csv_file"] == csv_file:
            logger.info(f"Loaded state for {csv_file}: bytes_read={state['bytes_read']}, record_count={state['record_count']}, chunk_index={state['chunk_index']}")
            return state["bytes_read"], state["record_count"], state["chunk_index"]
    return 0, 0, 0

# Hàm serialize JSON
def serialize_json(obj):
    return json.dumps(obj).encode('utf-8')

# Hàm kiểm tra Kafka sẵn sàng
def wait_for_kafka():
    max_attempts = 30
    delay = 5
    for attempt in range(max_attempts):
        try:
            producer = KafkaProducer(
                bootstrap_servers="data-lake-kafka-headless:9092",
                value_serializer=serialize_json,
                retries=5,
                retry_backoff_ms=1000,
                buffer_memory=33554432,
                batch_size=32768,  # Tăng batch size
                linger_ms=10  # Tăng linger_ms để tích lũy thêm record
            )
            logger.info("Kafka is ready!")
            return producer
        except NoBrokersAvailable:
            logger.warning(f"Attempt {attempt + 1}/{max_attempts}: Kafka not available, retrying in {delay}s...")
            time.sleep(delay)
    logger.error("Kafka not available after maximum attempts.")
    raise Exception("Kafka connection failed")

# Hàm tạo topic Kafka
def create_kafka_topic():
    max_attempts = 5
    delay = 5
    for attempt in range(max_attempts):
        try:
            admin_client = KafkaAdminClient(
                bootstrap_servers="data-lake-kafka-headless:9092"
            )
            topic_list = [
                NewTopic(
                    name=TOPIC,
                    num_partitions=3,
                    replication_factor=1
                )
            ]
            existing_topics = admin_client.list_topics()
            if TOPIC not in existing_topics:
                admin_client.create_topics(new_topics=topic_list, validate_only=False)
                logger.info(f"Created Kafka topic: {TOPIC}")
            else:
                logger.info(f"Kafka topic {TOPIC} already exists")
            admin_client.close()
            return
        except KafkaError as e:
            logger.warning(f"Attempt {attempt + 1}/{max_attempts}: Failed to create topic, retrying in {delay}s... Error: {e}")
            time.sleep(delay)
        except Exception as e:
            logger.error(f"Unexpected error creating topic: {e}")
            raise
    logger.error("Failed to create topic after maximum attempts.")
    raise Exception("Topic creation failed")

@retry((S3Error, IncompleteRead), tries=5, delay=10, backoff=2, logger=logger)
def get_chunk(csv_file, offset, length):
    response = minio_client.get_object(BUCKET_NAME, csv_file, offset=offset, length=length)
    try:
        return response
    except Exception as e:
        response.close()
        response.release_conn()
        raise e

@retry(KafkaError, tries=5, delay=1, backoff=2, logger=logger)
def send_batch_to_kafka(producer, topic, batch):
    for record in batch:
        producer.send(topic, value=record)
    logger.debug(f"Sent batch of {len(batch)} records")

# Hàm kiểm tra định dạng CSV
def check_csv_format(csv_file):
    response = minio_client.get_object(BUCKET_NAME, csv_file)
    try:
        first_chunk = response.read(2048).decode('utf-8-sig', errors='replace').strip()
        logger.info(f"First 100 chars of {csv_file}: {first_chunk[:100]}")
        lines = first_chunk.split('\n')
        if not lines:
            logger.error(f"Invalid CSV format in {csv_file}: Empty file")
            return False, 0
        first_line = lines[0].split(';')
        if len(first_line) != 10 or first_line[0] != "GHCN_DIN":
            logger.error(f"Invalid CSV format in {csv_file}: Expected 10 columns with header 'GHCN_DIN', got {len(first_line)}")
            return False, 0
        header_bytes = len(lines[0].encode('utf-8')) + 1
        logger.info(f"CSV format check passed for {csv_file}, header size: {header_bytes} bytes")
        return True, header_bytes
    except Exception as e:
        logger.error(f"Error checking CSV format for {csv_file}: {e}")
        return False, 0
    finally:
        response.close()
        response.release_conn()

# Hàm chuyển dòng CSV thành dict
def csv_row_to_dict(row):
    def parse_float(value):
        try:
            return float(value) if value.strip() else None
        except ValueError:
            return None

    try:
        lat, lon = map(float, row[8].split(',')) if row[8].strip() else (None, None)
    except (ValueError, IndexError):
        lat, lon = None, None

    return {
        "ghcn_din": row[0].strip() or None,
        "date": row[1].strip() or None,
        "prcp": parse_float(row[2]),
        "snow": parse_float(row[3]),
        "tmax": parse_float(row[4]),
        "tmin": parse_float(row[5]),
        "elevation": parse_float(row[6]),
        "name": row[7].strip() or None,
        "latitude": lat,
        "longitude": lon,
        "country_code": row[9].strip() or None
    }

def main():
    parser = argparse.ArgumentParser(description="Weather Data Processor")
    parser.add_argument('--file', required=True, help="CSV file name")
    args = parser.parse_args()
    csv_file = args.file

    try:
        create_kafka_topic()
        producer = wait_for_kafka()
    except Exception as e:
        logger.error(f"Failed to initialize Kafka: {e}")
        exit(1)

    logger.info("Sending data from MinIO to Kafka...")
    logger.info(f"Processing file: {csv_file}")
    is_valid, header_bytes = check_csv_format(csv_file)
    if not is_valid:
        logger.error(f"Skipping {csv_file} due to invalid CSV format")
        return

    bytes_read, record_count, chunk_index = load_state(csv_file)
    resume_attempts = 0
    invalid_row_count = 0  # Đếm số dòng lỗi liên tiếp
    last_bytes_read = bytes_read  # Theo dõi để phát hiện vòng lặp

    file_size = minio_client.stat_object(BUCKET_NAME, csv_file).size
    logger.info(f"File {csv_file} size: {file_size} bytes")

    while bytes_read < file_size and resume_attempts <= MAX_RESUME_ATTEMPTS:
        try:
            offset = bytes_read
            length = min(CHUNK_SIZE, file_size - offset)
            response = get_chunk(csv_file, offset, length)
            logger.info(f"READ OK chunk {chunk_index} from byte {offset}, length {length}")

            chunk_data = response.read().decode('utf-8-sig')
            response.close()
            response.release_conn()

            remaining = ""
            if chunk_data and not chunk_data.endswith('\n'):
                last_newline = chunk_data.rfind('\n')
                if last_newline != -1:
                    remaining = chunk_data[last_newline+1:]
                    chunk_data = chunk_data[:last_newline]
                else:
                    remaining = chunk_data
                    chunk_data = ""

            reader = csv.reader(StringIO(chunk_data), delimiter=';')
            if offset == 0:
                next(reader, None)  # Bỏ header
                logger.info(f"Skipped header for {csv_file}")

            batch = []
            for row in reader:
                if row[0].strip() == "GHCN_DIN":
                    logger.warning(f"Detected header row in {csv_file} at record {record_count}, skipping")
                    bytes_read += sum(len(cell.encode('utf-8')) + 1 for cell in row) + 1
                    continue
                if len(row) != 10:
                    logger.warning(f"Skipping invalid row in {csv_file} at byte {bytes_read}: {row}")
                    bytes_read += sum(len(cell.encode('utf-8')) + 1 for cell in row) + 1
                    invalid_row_count += 1
                    if invalid_row_count >= MAX_INVALID_ROWS:
                        logger.error(f"Too many consecutive invalid rows ({invalid_row_count}) at byte {bytes_read}, aborting")
                        raise Exception("Too many invalid rows detected")
                    continue
                invalid_row_count = 0  # Reset nếu dòng hợp lệ
                obj = csv_row_to_dict(row)
                batch.append(obj)
                record_count += 1
                bytes_read += sum(len(cell.encode('utf-8')) + 1 for cell in row) + 1

                if len(batch) >= BATCH_SIZE:
                    send_batch_to_kafka(producer, TOPIC, batch)
                    batch = []
                    if record_count % FLUSH_INTERVAL == 0:
                        producer.flush()
                        save_state(csv_file, bytes_read, record_count, chunk_index)
                        logger.info(f"Flushed Kafka producer after {record_count} records")
                    time.sleep(BATCH_DELAY)

            if batch:
                send_batch_to_kafka(producer, TOPIC, batch)
                if record_count % FLUSH_INTERVAL == 0:
                    producer.flush()
                    save_state(csv_file, bytes_read, record_count, chunk_index)
                    logger.info(f"Flushed Kafka producer after {record_count} records")
                time.sleep(BATCH_DELAY)

            chunk_index += 1
            if remaining:
                next_offset = offset + length
                next_length = min(CHUNK_SIZE, file_size - next_offset)
                if next_length > 0:
                    response = get_chunk(csv_file, next_offset, next_length)
                    next_data = response.read().decode('utf-8-sig')
                    response.close()
                    response.release_conn()
                    complete_line = remaining + next_data.split('\n', 1)[0]
                    reader = csv.reader(StringIO(complete_line), delimiter=';')
                    batch = []
                    for row in reader:
                        if row[0].strip() == "GHCN_DIN":
                            logger.warning(f"Detected header row in {csv_file} at record {record_count}, skipping")
                            bytes_read += sum(len(cell.encode('utf-8')) + 1 for cell in row) + 1
                            continue
                        if len(row) != 10:
                            logger.warning(f"Skipping invalid row in {csv_file} at byte {bytes_read}: {row}")
                            bytes_read += sum(len(cell.encode('utf-8')) + 1 for cell in row) + 1
                            invalid_row_count += 1
                            if invalid_row_count >= MAX_INVALID_ROWS:
                                logger.error(f"Too many consecutive invalid rows ({invalid_row_count}) at byte {bytes_read}, aborting")
                                raise Exception("Too many invalid rows detected")
                            continue
                        invalid_row_count = 0
                        obj = csv_row_to_dict(row)
                        batch.append(obj)
                        record_count += 1
                        bytes_read += sum(len(cell.encode('utf-8')) + 1 for cell in row) + 1
                    if batch:
                        send_batch_to_kafka(producer, TOPIC, batch)
                        time.sleep(BATCH_DELAY)

            save_state(csv_file, bytes_read, record_count, chunk_index)
            logger.info(f"Finished chunk {chunk_index} for {csv_file}, bytes_read={bytes_read}")

            # Kiểm tra nếu bị kẹt tại cùng vị trí byte
            if bytes_read == last_bytes_read:
                resume_attempts += 1
                logger.warning(f"No progress at byte {bytes_read}, attempt {resume_attempts}/{MAX_RESUME_ATTEMPTS}")
                if resume_attempts > MAX_RESUME_ATTEMPTS:
                    logger.error(f"Stuck at byte {bytes_read} after {MAX_RESUME_ATTEMPTS} attempts, aborting")
                    break
            else:
                resume_attempts = 0
                last_bytes_read = bytes_read

        except (S3Error, IncompleteRead, csv.Error, ValueError, IOError, OSError) as e:
            resume_attempts += 1
            if resume_attempts > MAX_RESUME_ATTEMPTS:
                logger.error(f"Max resume attempts reached for {csv_file}: {e}")
                break
            logger.warning(f"Error processing chunk {chunk_index} of {csv_file} at byte {bytes_read}, attempt {resume_attempts}/{MAX_RESUME_ATTEMPTS}: {e}")
            save_state(csv_file, bytes_read, record_count, chunk_index)
            time.sleep(15 * resume_attempts)
            continue
        except Exception as e:
            logger.error(f"Critical error processing {csv_file}: {e}")
            break

    if bytes_read >= file_size:
        producer.flush()
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        logger.info(f"Finished processing {csv_file}, sent {record_count} records")

    try:
        producer.flush()
        logger.info("Flushed Kafka producer")
    finally:
        producer.close()
        logger.info("Closed Kafka producer")

    logger.info("All data sent to Kafka topic: %s", TOPIC)

if __name__ == "__main__":
    main()