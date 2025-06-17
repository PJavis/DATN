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

# Khởi tạo SparkSession với tài nguyên tối ưu
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
        .config("spark.jars", "/opt/spark/jars/hadoop-aws-3.3.4.jar,/opt/spark/jars/aws-java-sdk-bundle-1.12.761.jar")
        .config("spark.sql.shuffle.partitions", "4")  # Giảm số phân vùng shuffle
        .config("spark.default.parallelism", "4")     # Giảm song song
        .config("spark.driver.memory", "2g")          # Tăng RAM driver
        .config("spark.executor.memory", "4g")        # Tăng RAM executor
        .config("spark.executor.cores", "2")          # Tăng CPU
        .config("spark.parquet.block.size", "33554432")  # Kích thước khối 32MB
        .config("spark.sql.adaptive.enabled", "true")    # Bật chế độ thích ứng
        .enableHiveSupport()
        .getOrCreate()
    )
    logger.info("Khởi tạo SparkSession thành công")
except Exception as e:
    logger.error(f"Lỗi khởi tạo SparkSession: {str(e)}")
    raise e

# Hàm đếm số file trong phân vùng
def get_partition_file_count(spark, partition_path):
    try:
        fs = spark._jvm.org.apache.hadoop.fs.FileSystem.get(spark._jsc.hadoopConfiguration())
        path = spark._jvm.org.apache.hadoop.fs.Path(partition_path)
        file_count = len([status for status in fs.listStatus(path) if not status.isDirectory()]) if fs.exists(path) else 0
        logger.info(f"Phát hiện {file_count} file trong {partition_path}")
        return file_count
    except Exception as e:
        logger.error(f"Lỗi đếm file trong {partition_path}: {str(e)}")
        return 0

# Hàm nén phân vùng
def compact_partition(country_code, year, month):
    partition_path = f"s3a://hive-warehouse/weather.db/weather_data/country_code={country_code}/year={year}/month={month}"
    file_count = get_partition_file_count(spark, partition_path)
    
    if file_count > 10:  # Tăng ngưỡng lên 10
        logger.info(f"Tiến hành nén phân vùng: country_code={country_code}, year={year}, month={month} với {file_count} file")
        try:
            # Tạo bảng tạm
            temp_table = f"temp_weather_{country_code}_{year}_{month}"
            df = spark.table("weather.weather_data").filter(
                (col("country_code") == country_code) & 
                (col("year") == year) & 
                (col("month") == month)
            ).select("station_id", "record_date", "temperature_max", "temperature_min", 
                     "precipitation", "snow", "elevation", "name", "latitude", "longitude")
            
            df.coalesce(1).write.mode("overwrite").saveAsTable(temp_table)
            spark.sql(f"INSERT OVERWRITE TABLE weather.weather_data PARTITION (country_code='{country_code}', year={year}, month={month}) SELECT * FROM {temp_table}")
            spark.sql(f"DROP TABLE {temp_table}")
            
            logger.info(f"Hoàn tất nén phân vùng: country_code={country_code}, year={year}, month={month}")
        except Exception as e:
            logger.error(f"Lỗi nén phân vùng {country_code}/{year}/{month}: {str(e)}")
            raise e
    else:
        logger.info(f"Không nén phân vùng: country_code={country_code}, year={year}, month={month} với {file_count} file")

# Lấy danh sách phân vùng (giảm xuống 1 để thử nghiệm)
try:
    logger.info("Lấy danh sách phân vùng từ weather.weather_data")
    partitions = spark.sql("""
        SELECT country_code, year, month
        FROM weather.weather_data
        GROUP BY country_code, year, month
        LIMIT 1  # Giảm xuống 1 phân vùng để thử nghiệm
    """).collect()
except Exception as e:
    logger.error(f"Lỗi lấy danh sách phân vùng: {str(e)}")
    partitions = []

# Nén từng phân vùng
for row in partitions:
    compact_partition(row["country_code"], row["year"], row["month"])

# Dừng SparkSession
try:
    logger.info("Dừng SparkSession")
    spark.stop()
except Exception as e:
    logger.error(f"Lỗi dừng SparkSession: {str(e)}")
    raise e