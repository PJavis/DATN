#!/bin/bash

# Tên Helm Umbrella Chart
PROJECT_NAME="data-lake-project"

# Danh sách các subcharts
SUBCHARTS=("minio" "hive" "trino" "superset" "spark" "kafka" "airflow" "postgres")

# Tạo thư mục dự án
mkdir -p $PROJECT_NAME
cd $PROJECT_NAME || { echo "❌ Lỗi: Không thể vào thư mục $PROJECT_NAME"; exit 1; }

# Khởi tạo Helm Umbrella Chart
helm create temp-chart
rm -rf temp-chart/templates/*
mv temp-chart/* .
rmdir temp-chart

# Xóa template không cần thiết
rm -rf templates/*

# Tạo thư mục chứa các subcharts
mkdir -p charts

# Tạo từng subchart
for chart in "${SUBCHARTS[@]}"; do
    echo "🚀 Đang tạo subchart: $chart..."
    helm create charts/$chart
    rm -rf charts/$chart/templates/*  # Xóa template mặc định

    # Tạo file ingress.yaml cho subchart (trừ postgres)
    if [ "$chart" != "postgres" ]; then
        cat <<EOF > charts/$chart/templates/ingress.yaml
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ .Release.Name }}-$chart
  annotations:
    {{- toYaml .Values.ingress.annotations | nindent 4 }}
spec:
  ingressClassName: {{ .Values.ingress.className }}
  rules:
  - host: {{ .Values.ingress.hosts.$chart }}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: {{ .Release.Name }}-$chart
            port:
              number: 80
{{- end }}
EOF
    fi

    # Cấu hình values.yaml cho subchart
    if [ "$chart" != "postgres" ]; then
        cat <<EOF > charts/$chart/values.yaml
ingress:
  enabled: true
  className: "nginx"
  annotations: {}
  hosts:
    $chart: $chart.local
  tls: []
EOF
    else
        # Cấu hình riêng cho postgres không có ingress
        cat <<EOF > charts/$chart/values.yaml
postgresql:
  enabled: true
  auth:
    username: airflow
    password: airflow
    database: airflow
  primary:
    service:
      ports:
        postgresql: 5432
EOF
    fi
done

# Cấu hình Chart.yaml cho Umbrella Chart
cat <<EOF > Chart.yaml
apiVersion: v2
name: $PROJECT_NAME
description: Umbrella Helm Chart for Data Lake
type: application
version: 0.1.0
appVersion: "1.0"
dependencies:
EOF

# Thêm danh sách subcharts vào Chart.yaml
for chart in "${SUBCHARTS[@]}"; do
    echo "  - name: $chart" >> Chart.yaml
    echo "    version: 0.1.0" >> Chart.yaml
    echo "    repository: \"file://charts/$chart\"" >> Chart.yaml
done

# Cấu hình values.yaml cho Umbrella Chart
cat <<EOF > values.yaml
ingress:
  enabled: true
  className: "nginx"
  annotations:
    kubernetes.io/ingress.class: "nginx"
    nginx.ingress.kubernetes.io/rewrite-target: /
  hosts:
    minio: minio.local
    hive: hive.local
    trino: trino.local
    superset: superset.local
    spark: spark.local
    kafka: kafka.local
    airflow: airflow.local
  tls: []

airflow:
  database:
    host: "{{ .Release.Name }}-postgres"
    port: 5432
    name: airflow
    user: airflow
    password: airflow
EOF

echo "✅ Completed Helm Umbrella Chart!"
echo "👉 Run 'helm dependency update' to update dependencies."