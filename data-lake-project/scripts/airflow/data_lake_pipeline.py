from datetime import datetime
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
import subprocess

default_args = {
    'owner': 'Nguyen-Quoc-Dung',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 3,
    'retry_delay': 300,
}

### ===== DAG 1: data_lake_pipeline =====

def run_upload_to_minio():
    subprocess.run("python /opt/airflow/dags/upload_to_minio.py", shell=True, check=True)

def run_weather_processor(year):
    subprocess.run(
        f"python /opt/airflow/weather_processor.py --file noaa-daily-weather-data-{year}.csv",
        shell=True,
        check=True
    )

dag1 = DAG(
    'data_lake_pipeline',
    default_args=default_args,
    description='One-time load and extract weather data',
    schedule_interval=None,  # chạy tay
    start_date=days_ago(1),
    catchup=False,
)

with dag1:
    load_data = PythonOperator(
        task_id="load_data",
        python_callable=run_upload_to_minio,
    )

    years = range(2015, 2021)
    extract_data_tasks = [
        PythonOperator(
            task_id=f"extract_data_{year}",
            python_callable=run_weather_processor,
            op_kwargs={"year": year}
        ) for year in years
    ]

    trigger_processing = TriggerDagRunOperator(
        task_id="trigger_process_and_analyze",
        trigger_dag_id="process_and_analyze_every_3h",
        wait_for_completion=False
    )

    load_data >> extract_data_tasks >> trigger_processing


### ===== DAG 2: process_and_analyze_every_3h =====

dag2 = DAG(
    'process_and_analyze_every_3h',
    default_args=default_args,
    description='Run Spark + ANALYZE every 3 hours',
    schedule_interval="0 */3 * * *",
    start_date=days_ago(1),
    catchup=False,
)

with dag2:
    process_and_analyze_data = BashOperator(
        task_id='process_and_analyze_data',
        bash_command="""\
        /opt/spark/bin/spark-submit \
            --master spark://data-lake-spark-master:7077 \
            --deploy-mode client \
            --conf spark.driver.bindAddress=0.0.0.0 \
            --conf spark.driver.host=spark-driver-service.default.svc.cluster.local \
            --conf spark.driver.port=7077 \
            --conf spark.blockManager.port=7079 \
            --conf spark.port.maxRetries=32 \
            --jars /opt/spark/jars/spark-sql-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/kafka-clients-3.4.1.jar,/opt/spark/jars/spark-streaming-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/spark-token-provider-kafka-0-10_2.12-3.5.5.jar,/opt/spark/jars/hadoop-aws-3.3.4.jar,/opt/spark/jars/aws-java-sdk-bundle-1.12.761.jar \
            /opt/spark/work-dir/compact_parquet.py && \
        curl -X POST http://data-lake-trino-coordinator:8082/v1/statement \
            -H "X-Trino-User: admin" \
            -H "X-Trino-Catalog: hive" \
            -H "X-Trino-Schema: weather" \
            -H "Content-Type: application/json" \
            -d '{"query": "ANALYZE weather_data"}'
        """,
        env={
            'AWS_ACCESS_KEY_ID': 'minio',
            'AWS_SECRET_ACCESS_KEY': 'minio123',
            'AWS_DEFAULT_REGION': 'us-east-1',
        },
    )

