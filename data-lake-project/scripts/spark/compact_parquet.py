from pyspark.sql import SparkSession
from pyspark.sql.functions import col
import logging
import sys

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("WeatherParquetCompaction")

# Khởi tạo SparkSession
try:
    logger.info("Khởi tạo SparkSession")
    spark = (
        SparkSession.builder
        .appName("WeatherParquetCompaction")
        .config("spark.hadoop.fs.s3a.endpoint", "http://data-lake-minio-headless:9000")
        .config("spark.hadoop.fs.s3a.access.key", "minio")
        .config("spark.hadoop.fs.s3a.secret.key", "minio123")
        .config("spark.hadoop.fs.s3a.path.style.access", "true")
        .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem")
        .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
        .config("spark.hadoop.fs.s3a.region", "us-east-1")
        .config("spark.hadoop.fs.s3a.aws.credentials.provider", "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider")
        .config("spark.sql.catalogImplementation", "hive")
        .config("spark.sql.hive.metastore.uris", "thrift://data-lake-hive:9083")
        .config("spark.sql.hive.metastore.version", "3.1.3")
        .config("spark.hadoop.hive.metastore.sasl.enabled", "false")
        .config("spark.jars", "/opt/spark/jars/postgresql-42.7.3.jar,/opt/spark/jars/hadoop-aws-3.3.4.jar,/opt/spark/jars/aws-java-sdk-bundle-1.12.761.jar,/opt/spark/jars/spark-sql-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/kafka-clients-3.4.1.jar,/opt/spark/jars/spark-streaming-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/spark-token-provider-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/jsr305-3.0.0.jar,/opt/spark/jars/commons-pool2-2.11.1.jar,/opt/spark/jars/lz4-java-1.8.0.jar,/opt/spark/jars/snappy-java-1.1.10.5.jar")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.parquet.block.size", "67108864")
        .enableHiveSupport()
        .getOrCreate()
    )
    logger.info("Khởi tạo SparkSession thành công")
except Exception as e:
    logger.error(f"Lỗi khi khởi tạo SparkSession: {str(e)}")
    raise e

# Tạo bảng tạm
try:
    logger.info("Tạo bảng tạm weather.weather_data_temp")
    spark.sql("""
        CREATE TABLE IF NOT EXISTS weather.weather_data_temp (
            station_id STRING,
            record_date DATE,
            temperature_max DOUBLE,
            precipitation DOUBLE,
            snow DOUBLE,
            temperature_min DOUBLE,
            elevation DOUBLE,
            name STRING,
            latitude DOUBLE,
            longitude DOUBLE
        )
        PARTITIONED BY (country_code STRING, year INT, month INT)
        STORED AS PARQUET
        LOCATION 's3a://hive-warehouse/weather.db/weather_data_temp'
    """)
except Exception as e:
    logger.error(f"Lỗi khi tạo bảng tạm: {str(e)}")
    raise e

# Hàm đếm số lượng tệp trong phân vùng
def get_partition_file_count(spark, partition_path):
    try:
        # Đếm tệp Parquet bằng spark.read.parquet
        df = spark.read.parquet(partition_path)
        file_count = df.inputFiles().length  # Đếm số tệp đầu vào
        logger.info(f"Tìm thấy {file_count} tệp trong {partition_path}")
        return file_count
    except Exception as e:
        logger.error(f"Lỗi khi đếm tệp trong {partition_path}: {str(e)}")
        return 0

# Hàm nén phân vùng
def compact_partition(country_code, year, month):
    partition_path = f"s3a://hive-warehouse/weather.db/weather_data/country_code={country_code}/year={year}/month={month}"
    file_count = get_partition_file_count(spark, partition_path)
    
    if file_count > 10:
        logger.info(f"Nén phân vùng: country_code={country_code}, year={year}, month={month} với {file_count} tệp")
        try:
            df = spark.table("weather.weather_data").filter(
                (col("country_code") == country_code) & 
                (col("year") == year) & 
                (col("month") == month)
            )
            
            # Chia lại phân vùng và sắp xếp để tối ưu truy vấn
            df = df.repartition(4).sortWithinPartitions("record_date", "station_id")
            
            # Ghi vào bảng tạm
            df.write.partitionBy("country_code", "year", "month") \
                .bucketBy(8, "station_id") \
                .format("parquet") \
                .option("compression", "snappy") \
                .mode("overwrite") \
                .save(f"s3a://hive-warehouse/weather.db/weather_data_temp/country_code={country_code}/year={year}/month={month}")
            
            # Cập nhật metadata Hive cho bảng tạm
            spark.sql(f"""
                ALTER TABLE weather.weather_data_temp
                ADD IF NOT EXISTS PARTITION (
                    country_code='{country_code}',
                    year={year},
                    month={month}
                )
                LOCATION 's3a://hive-warehouse/weather.db/weather_data_temp/country_code={country_code}/year={year}/month={month}'
            """)
            
            # Chuyển dữ liệu từ bảng tạm sang bảng chính
            spark.sql(f"""
                INSERT OVERWRITE TABLE weather.weather_data
                PARTITION (country_code='{country_code}', year={year}, month={month})
                SELECT * FROM weather.weather_data_temp
                WHERE country_code='{country_code}' AND year={year} AND month={month}
            """)
            
            # Xóa phân vùng cũ trong bảng tạm
            spark.sql(f"""
                ALTER TABLE weather.weather_data_temp
                DROP IF EXISTS PARTITION (
                    country_code='{country_code}',
                    year={year},
                    month={month}
                )
            """)
            
            logger.info(f"Đã nén phân vùng: country_code={country_code}, year={year}, month={month}")
        except Exception as e:
            logger.error(f"Lỗi khi nén phân vùng {country_code}/{year}/{month}: {str(e)}")
            raise e
    else:
        logger.info(f"Bỏ qua phân vùng: country_code={country_code}, year={year}, month={month} với {file_count} tệp")

# Lấy danh sách phân vùng
try:
    logger.info("Lấy danh sách phân vùng từ weather.weather_data")
    partitions = spark.sql("SHOW PARTITIONS weather.weather_data").collect()
except Exception as e:
    logger.error(f"Lỗi khi lấy danh sách phân vùng: {str(e)}")
    partitions = []

# Nén từng phân vùng
for row in partitions:
    partition_spec = row["partition"]
    parts = dict(p.split("=") for p in partition_spec.split("/"))
    country_code = parts["country_code"]
    year = int(parts["year"])
    month = int(parts["month"])
    compact_partition(country_code, year, month)

# Cập nhật metadata
try:
    logger.info("Chạy MSCK REPAIR TABLE")
    spark.sql("MSCK REPAIR TABLE weather.weather_data")
except Exception as e:
    logger.error(f"Lỗi khi chạy MSCK REPAIR TABLE: {str(e)}")
    raise e

# Xóa bảng tạm
try:
    logger.info("Xóa bảng tạm weather.weather_data_temp")
    spark.sql("DROP TABLE IF EXISTS weather.weather_data_temp")
except Exception as e:
    logger.error(f"Lỗi khi xóa bảng tạm: {str(e)}")
    raise e

# Dừng SparkSession
try:
    logger.info("Dừng SparkSession")
    spark.stop()
except Exception as e:
    logger.error(f"Lỗi khi dừng SparkSession: {str(e)}")
    raise e