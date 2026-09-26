#!/bin/sh
set -e

# TODO: This is not needed for singularity, fix later so it works for other providers
# SERVICE_IP=$(ping -c 1 ${SERVER_HOSTNAME:-localhost} | grep PING | awk -F'[()]' '{print $2}')
# if ! grep -q "$SERVICE_IP the-agent-company.com" /etc/hosts; then
#     echo "$SERVICE_IP the-agent-company.com" | sudo tee -a /etc/hosts
# else
#     echo "The entry '$SERVICE_IP the-agent-company.com' already exists in /etc/hosts."
# fi

echo "Resetting services..."

# OWNCLOUD_INSTANCE_SUFFIX: empty for instance 1 (default), "-2" for instance 2, "-3" for instance 3
SUFFIX="${OWNCLOUD_INSTANCE_SUFFIX:-}"

# Initialize array to track which services need resetting
reset_services=()

# Function to check if a service is healthy
check_service_health() {
    local service=$1
    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" -I "http://${SERVER_HOSTNAME}:2999/api/healthcheck/${service}")
    echo "Service $service healthcheck status: $http_status"
    [ "$http_status" = "200" ]
}

# Function to wait for services to be ready
wait_for_services() {
    local max_attempts=180  # 15 minutes maximum wait time (180 * 5 seconds)
    local attempt=1
    local all_services_ready

    echo "Waiting for services to be ready..."

    while [ $attempt -le $max_attempts ]; do
        all_services_ready=true

        for service in "${reset_services[@]}"; do
            if ! check_service_health "$service"; then
                echo "Service $service not ready yet (attempt $attempt)..."
                all_services_ready=false
                break
            fi
        done

        if [ "$all_services_ready" = true ]; then
            echo "All services are ready!"
            return 0
        fi

        echo "Waiting 5 seconds before next check..."
        sleep 5
        attempt=$((attempt + 1))
    done

    echo "Error: Timeout waiting for services to be ready"
    return 1
}

# Check and reset each service
for service in owncloud; do
    if grep -q "$service" dependencies.yml; then
        service_with_suffix="${service}${SUFFIX}"

        # The stock TheAgentCompany api-server image only knows the slot-1 service name:
        # /api/healthcheck/owncloud-2 and -3 return 404, and waiting on a route that will
        # never answer used to burn the full 15-minute timeout and then fail the run. Probe
        # first and skip the API reset for the slots it does not serve -- the file tree is
        # restored per task by owncloud_reset_state() in injection.py either way, which is
        # what actually makes the environment clean.
        probe=$(curl -s -o /dev/null -w "%{http_code}" -I "http://${SERVER_HOSTNAME}:2999/api/healthcheck/${service_with_suffix}")
        if [ "$probe" = "404" ]; then
            echo "api-server has no route for ${service_with_suffix} (404); skipping its API reset."
            continue
        fi

        echo "Resetting ${service_with_suffix}..."
        curl -X POST "http://${SERVER_HOSTNAME}:2999/api/reset-${service_with_suffix}"
        reset_services+=("${service_with_suffix}")
    fi
done

# If any services were reset, wait for them to be ready
if [ ${#reset_services[@]} -gt 0 ]; then
    echo "Reset initiated for services: ${reset_services[*]}"
    wait_for_services
else
    echo "No matching services found in dependencies.yml"
fi