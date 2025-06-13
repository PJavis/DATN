#!/bin/bash

CLUSTER_NAME="my-cluster"
MOUNT_SOURCE="/home/nguyen-quoc-dung/Downloads/DATN"
LOCAL_SOURCE="$HOME/data"
NAMESPACE="default"

# Ensure MOUNT_SOURCE exists and contains CSV files
echo "Checking CSV files in $MOUNT_SOURCE..."
if [ ! -d "$MOUNT_SOURCE" ]; then
  echo "Error: Directory $MOUNT_SOURCE does not exist"
  exit 1
fi
if ! ls "$MOUNT_SOURCE"/noaa-daily-weather-data-*.csv >/dev/null 2>&1; then
  echo "Error: No CSV files found in $MOUNT_SOURCE"
  ls -l "$MOUNT_SOURCE"
  exit 1
fi
echo "Found CSV files:"
ls -l "$MOUNT_SOURCE"/noaa-daily-weather-data-*.csv

# Copy files to local source
echo "Copying files to $LOCAL_SOURCE..."
mkdir -p "$LOCAL_SOURCE/pvc"
cp "$MOUNT_SOURCE"/noaa-daily-weather-data-*.csv "$LOCAL_SOURCE/pvc/"
if [ $? -ne 0 ]; then
  echo "Error: Failed to copy files to $LOCAL_SOURCE/pvc"
  exit 1
fi
chmod -R 755 "$LOCAL_SOURCE/pvc"
echo "Files in $LOCAL_SOURCE/pvc:"
ls -l "$LOCAL_SOURCE/pvc"/noaa-daily-weather-data-*.csv

# Check Minikube cluster status
echo "Checking Minikube cluster '$CLUSTER_NAME' status..."
if ! minikube -p "$CLUSTER_NAME" status >/dev/null 2>&1 || [ "$(minikube -p "$CLUSTER_NAME" status | grep -c 'Running')" -lt 3 ]; then
  echo "Starting cluster..."
  minikube start -p "$CLUSTER_NAME" \
    --nodes 4 \
    --driver=docker \
    --cpus 2 \
    --memory 5000 \
    --extra-config=apiserver.request-timeout=60s
  if [ $? -ne 0 ]; then
    echo "Error: Failed to start Minikube cluster"
    exit 1
  fi
else
  echo "Minikube cluster '$CLUSTER_NAME' is already running."
fi

# Create PersistentVolumeClaim
echo "Creating PersistentVolumeClaim (data-pvc)..."
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-pvc
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/managed-by: Helm
  annotations:
    meta.helm.sh/release-name: data-lake
    meta.helm.sh/release-namespace: $NAMESPACE
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
EOF

# Verify PersistentVolumeClaim
echo "Verifying PersistentVolumeClaims..."
for pvc in data-pvc; do
  echo "Checking status of $pvc..."
  for i in {1..30}; do
    if kubectl get pvc $pvc -n "$NAMESPACE" -o jsonpath='{.status.phase}' | grep -q "Bound"; then
      echo "$pvc is Bound"
      break
    fi
    echo "Waiting for $pvc to bind ($i/30)..."
    sleep 10
  done
  if ! kubectl get pvc $pvc -n "$NAMESPACE" -o jsonpath='{.status.phase}' | grep -q "Bound"; then
    echo "Error: $pvc not bound after 5 minutes"
    kubectl describe pvc $pvc -n "$NAMESPACE"
    exit 1
  fi
done

# Create temporary pod to copy files
echo "Creating temporary pod to copy files..."
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: copy-files-to-pvc
  namespace: $NAMESPACE
spec:
  containers:
  - name: copy-files
    image: busybox
    command: ["/bin/sh", "-c"]
    args:
    - |
      echo "Listing /data before copying..."
      ls -l /data
      sleep 7200
    volumeMounts:
    - name: data-pvc
      mountPath: /data
  volumes:
  - name: data-pvc
    persistentVolumeClaim:
      claimName: data-pvc
  restartPolicy: Never
EOF

# Wait for pod to be ready
echo "Waiting for pod to be ready..."
kubectl wait --for=condition=ready pod/copy-files-to-pvc -n "$NAMESPACE" --timeout=2m
if [ $? -ne 0 ]; then
  echo "Error: Pod copy-files-to-pvc not ready"
  kubectl describe pod -n "$NAMESPACE" copy-files-to-pvc
  exit 1
fi

# Copy CSV files to data-pvc and verify
echo "Copying CSV files to data-pvc..."
for file in "$LOCAL_SOURCE/pvc"/noaa-daily-weather-data-*.csv; do
  filename=$(basename "$file")
  echo "Copying $filename to data-pvc..."
  kubectl cp "$file" "$NAMESPACE/copy-files-to-pvc:/data/$filename" --retries=3
  if [ $? -ne 0 ]; then
    echo "Error: Failed to copy $filename to data-pvc"
    exit 1
  fi
  echo "Copied $filename to data-pvc"
done

# Verify files in PVC
echo "Verifying files in data-pvc..."
kubectl exec -n "$NAMESPACE" copy-files-to-pvc -- ls -l /data
if [ $? -ne 0 ]; then
  echo "Error: Failed to list files in data-pvc"
  exit 1
fi

# Set permissions and ownership
echo "Setting permissions and ownership..."
kubectl exec -n "$NAMESPACE" copy-files-to-pvc -- sh -c "chown -R 50000:50000 /data && chmod -R 755 /data"
if [ $? -ne 0 ]; then
  echo "Error: Failed to set permissions and ownership"
  exit 1
fi

echo "Listing files in data-pvc after setting permissions..."
kubectl exec -n "$NAMESPACE" copy-files-to-pvc -- ls -l /data

# Clean up temporary pod
echo "Cleaning up temporary pod..."
kubectl delete pod -n "$NAMESPACE" copy-files-to-pvc --wait=true

# # Build Docker images
# echo "Building Docker images..."
# docker build -t custom-spark:3.5.1 -f data-lake-project/scripts/spark/Dockerfile.spark .
# docker build -t custom-hive:0.0.1 -f data-lake-project/scripts/hive/Dockerfile.hive .
# docker build -t custom-trino:0.0.1 -f data-lake-project/scripts/trino/Dockerfile.trino .
# docker build -t custom-airflow:2.7.1 -f data-lake-project/scripts/airflow/Dockerfile.airflow .

# # Load images into Minikube
# echo "Loading images into Minikube..."
# minikube -p "$CLUSTER_NAME" image load custom-spark:3.5.1
# minikube -p "$CLUSTER_NAME" image load custom-hive:0.0.1
# minikube -p "$CLUSTER_NAME" image load custom-trino:0.0.1
# minikube -p "$CLUSTER_NAME" image load custom-airflow:2.7.1

# Update Helm dependencies
cd data-lake-project || exit
echo "Updating Helm dependencies..."
helm dependency update
cd .. || exit

# Install kubernetes-dashboard
echo "Installing kubernetes-dashboard..."
helm repo add kubernetes-dashboard https://kubernetes.github.io/dashboard/
helm upgrade --install kubernetes-dashboard kubernetes-dashboard/kubernetes-dashboard --create-namespace --namespace kubernetes-dashboard

# Install or upgrade Helm release
if helm list -n "$NAMESPACE" | grep -q "data-lake"; then
  echo "Upgrading data-lake release..."
  helm upgrade data-lake data-lake-project --timeout 15m --namespace "$NAMESPACE"
else
  echo "Installing data-lake release..."
  helm install data-lake data-lake-project --timeout 15m --namespace "$NAMESPACE"
fi

# Clean up local source
echo "Cleaning up $LOCAL_SOURCE..."
rm -rf "$LOCAL_SOURCE"

# Wait for MinIO instances
echo "Waiting for 4 MinIO instances to be ready..."
kubectl wait --for=condition=ready pod -l app=data-lake-minio --namespace "$NAMESPACE" --timeout=10m
MINIO_PODS=$(kubectl get pods -n "$NAMESPACE" -l app=data-lake-minio --no-headers | wc -l)
if [ "$MINIO_PODS" -ne 4 ]; then
  echo "Error: Only $MINIO_PODS MinIO instances found, expected 4."
  exit 1
fi
echo "All 4 MinIO instances are running."

# Wait for Kafka instances
echo "Waiting for 3 Kafka instances to be ready..."
kubectl wait --for=condition=ready pod -l app=data-lake-kafka --namespace "$NAMESPACE" --timeout=10m
KAFKA_PODS=$(kubectl get pods -n "$NAMESPACE" -l app=data-lake-kafka --no-headers | wc -l)
if [ "$KAFKA_PODS" -ne 3 ]; then
  echo "Error: Only $KAFKA_PODS Kafka instances found, expected 3."
  exit 1
fi
echo "All 3 Kafka instances are running."

# Wait for Airflow deployment
echo "Waiting for Airflow deployment to be ready..."
kubectl wait --for=condition=available deployment -l app=data-lake-airflow --namespace "$NAMESPACE" --timeout=10m
AIRFLOW_PODS=$(kubectl get pods -n "$NAMESPACE" -l app=data-lake-airflow --no-headers | wc -l)
if [ "$AIRFLOW_PODS" -lt 1 ]; then
  echo "Error: Only $AIRFLOW_PODS Airflow pods found, expected at least 1."
  exit 1
fi
echo "All Airflow pods are running."

# Verify Airflow pod and DAGs
AIRFLOW_POD=$(kubectl get pods -n "$NAMESPACE" -l app=data-lake-airflow -o jsonpath="{.items[0].metadata.name}")
echo "Verifying DAG files in Airflow pod..."
kubectl exec -n "$NAMESPACE" "$AIRFLOW_POD" -c scheduler -- ls -l /opt/airflow/dags
if [ $? -ne 0 ]; then
  echo "Error: Failed to list DAG files in Airflow pod"
  exit 1
fi

# Verify CSV files in /mnt/data
echo "Verifying CSV files in /mnt/data of Airflow scheduler..."
kubectl exec -n "$NAMESPACE" "$AIRFLOW_POD" -c scheduler -- ls -l /mnt/data
if [ $? -ne 0 ]; then
  echo "Error: Failed to list CSV files in /mnt/data"
  exit 1
fi

# Wait for Airflow scheduler to sync DAGs
echo "Waiting for Airflow scheduler to sync DAGs..."
sleep 30

# List DAGs
echo "Listing DAGs in Airflow..."
kubectl exec -n "$NAMESPACE" "$AIRFLOW_POD" -c scheduler -- airflow dags list

# Trigger DAG
echo "Triggering data_lake_pipeline DAG..."
if ! kubectl exec -n "$NAMESPACE" "$AIRFLOW_POD" -c scheduler -- airflow dags trigger data_lake_pipeline; then
  echo "Error: Failed to trigger DAG"
  kubectl logs -n "$NAMESPACE" "$AIRFLOW_POD" -c scheduler | grep -i error
  exit 1
fi
echo "Successfully triggered data_lake_pipeline DAG"

# Output Kubernetes Dashboard token
echo "Token for kubernetes-dashboard:"
kubectl -n kubernetes-dashboard create token admin-user

# Port-forward for Kubernetes Dashboard
kubectl -n kubernetes-dashboard port-forward svc/kubernetes-dashboard-kong-proxy 8443:443

# trino://admin@data-lake-trino-coordinator:8082/hive/weather
