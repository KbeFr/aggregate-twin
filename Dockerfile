# syntax=docker/dockerfile:1
#
# aggregate_twin/Dockerfile
#

FROM python:3.11-slim AS base


WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# --- Shared/internal packages -------------------------------------------

COPY core_msgs/ /app/core_msgs
RUN pip install --no-cache-dir -e /app/core_msgs

COPY flexCommunicator/ /app/flexCommunicator
# First install requirements to be sure
RUN pip install --no-cache-dir -r /app/flexCommunicator/requirements.txt

RUN pip install --no-cache-dir -e /app/flexCommunicator


COPY aggregate_twin/ /app/aggregate_twin

WORKDIR /app/aggregate_twin
RUN pip install --no-cache-dir -r requirements.txt




ENV TWIN_TICK_HZ=10 \
    TWIN_NAMESPACE=default_ns \
    TWIN_NAME=AggregateTwin \
    PERCEPTION_SOURCE=static

CMD ["python", "main.py"]

ENV PYTHONUNBUFFERED=1
