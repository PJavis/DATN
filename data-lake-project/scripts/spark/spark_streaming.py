from pyspark.sql import SparkSession
from pyspark.sql.functions import col, year, month, dayofmonth, when, from_json
from pyspark.sql.types import StructType, StructField, StringType, FloatType, IntegerType

# =========================
# 1. SparkSession
# =========================
spark = (
    SparkSession.builder.appName("WeatherStreamingToMinIO")
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
    .config("spark.sql.hive.metastore.jars", "file://opt/spark/jars/*")
    .config("spark.hadoop.hive.metastore.sasl.enabled", "false")
    .config("spark.jars", "/opt/spark/jars/spark-sql-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/hadoop-aws-3.3.4.jar,/opt/spark/jars/aws-java-sdk-bundle-1.12.761.jar,/opt/spark/jars/postgresql-42.7.3.jar,/opt/spark/jars/kafka-clients-3.4.1.jar,/opt/spark/jars/spark-streaming-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/spark-token-provider-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/jsr305-3.0.0.jar,/opt/spark/jars/commons-pool2-2.11.1.jar,/opt/spark/jars/lz4-java-1.8.0.jar,/opt/spark/jars/snappy-java-1.1.10.5.jar")
    .enableHiveSupport()
    .getOrCreate()
)

# spark.sparkContext.setLogLevel("DEBUG")

# Kiểm tra Kafka source
print("Checking Kafka Source Provider:")
try:
    spark.readStream.format("kafka") \
        .option("kafka.bootstrap.servers", "data-lake-kafka-headless:9092") \
        .option("subscribe", "weather-topic") \
        .load()
    print("Kafka source loaded successfully")
except Exception as e:
    print(f"Error loading Kafka source: {str(e)}")

# =========================
# 2. Schema cho Kafka message
# =========================
schema = StructType(
    [
        StructField("ghcn_din", StringType(), False),
        StructField("date", StringType(), False),
        StructField("prcp", FloatType(), True),
        StructField("snow", FloatType(), True),
        StructField("tmax", FloatType(), True),
        StructField("tmin", FloatType(), True),
        StructField("elevation", FloatType(), True),
        StructField("name", StringType(), True),
        StructField("latitude", FloatType(), True),
        StructField("longitude", FloatType(), True),
        StructField("country_code", StringType(), False),
    ]
)

# =========================
# 3. Đọc Kafka
# =========================
kafka_df = (
    spark.readStream.format("kafka")
    .option("kafka.bootstrap.servers", "data-lake-kafka-headless:9092")
    .option("subscribe", "weather-topic")
    .option("startingOffsets", "latest")
    .option("failOnDataLoss", "false")
    .option("maxOffsetsPerTrigger", 100000)  # Giới hạn 100k record mỗi batch
    .load()
)

# Parse dữ liệu
parsed_df = (
    kafka_df.selectExpr("CAST(value AS STRING)")
    .select(from_json(col("value"), schema).alias("data"))
    .select("data.*")
)

# Xử lý dữ liệu
processed_df = (
    parsed_df
    .withColumn("prcp", when(col("prcp").isNull(), 0.0).otherwise(col("prcp")))
    .withColumn("snow", when(col("snow").isNull(), 0.0).otherwise(col("snow")))
    .withColumn("tmax", when(col("tmax").isNull(), 0.0).otherwise(col("tmax")))
    .withColumn("tmin", when(col("tmin").isNull(), 0.0).otherwise(col("tmin")))
    .withColumn("elevation", when(col("elevation").isNull(), 0.0).otherwise(col("elevation")))
    .withColumn("name", when(col("name").isNull(), "").otherwise(col("name")))
    .withColumn("latitude", when(col("latitude").isNull(), 0.0).otherwise(col("latitude")))
    .withColumn("longitude", when(col("longitude").isNull(), 0.0).otherwise(col("longitude")))
    .withColumn("country_code", when(col("country_code").isNull(), "UNKNOWN").otherwise(col("country_code")))
    .withColumn("record_date", col("date").cast("date"))  # Cast sang DATE
    .withColumn("year", year(col("record_date")))
    .withColumn("month", month(col("record_date")))
    .select(
        col("ghcn_din").alias("station_id"),
        col("record_date"),
        col("tmax").alias("temperature_max"),
        col("prcp").alias("precipitation"),
        col("snow"),
        col("tmin").alias("temperature_min"),
        col("elevation"),
        col("name"),
        col("latitude"),
        col("longitude"),
        col("country_code"),
        col("year"),
        col("month")
    )
)

output_df = processed_df.repartition(4)

# Xử lý batch
def process_batch(batch_df, batch_id):
    print(f"Processing batch {batch_id}...")
    try:
        batch_df.write.partitionBy("country_code", "year", "month") \
            .format("parquet") \
            .option("compression", "snappy") \
            .mode("append") \
            .save("s3a://hive-warehouse/weather.db/weather_data")
        print(f"Batch {batch_id} saved to MinIO at s3a://hive-warehouse/weather.db/weather_data")

        partitions = batch_df.select("country_code", "year", "month").distinct().collect()
        for row in partitions:
            spark.sql(f"""
                ALTER TABLE weather.weather_data
                ADD IF NOT EXISTS PARTITION (
                    country_code='{row["country_code"]}',
                    year={row["year"]},
                    month={row["month"]}
                )
                LOCATION 's3a://hive-warehouse/weather.db/weather_data/country_code={row["country_code"]}/year={row["year"]}/month={row["month"]}'
            """)
        print(f"Batch {batch_id} partitions updated in Hive")
    except Exception as e:
        print(f"Error in batch {batch_id}: {str(e)}")

# WriteStream
query = (
    output_df.writeStream.foreachBatch(process_batch)
    .option("checkpointLocation", "s3a://hive-warehouse/spark/checkpoint")
    .outputMode("append")
    .trigger(processingTime="5 seconds")
    .start()
)

query.awaitTermination()