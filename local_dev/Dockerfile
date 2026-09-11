FROM python:3.11-slim AS base


WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install flexCommunicator requirements
COPY flexCommunicator/requirements.txt /app/flexCommunicator/requirements.txt
RUN pip install --no-cache-dir -r /app/flexCommunicator/requirements.txt

# Install aggregate_twin requirements
COPY aggregate_twin/requirements.txt /app/aggregate_twin/requirements.txt
RUN pip install --no-cache-dir -r /app/aggregate_twin/requirements.txt

# ---  install local editable packages
COPY core_msgs/ /app/core_msgs
RUN pip install --no-cache-dir -e /app/core_msgs

COPY flexCommunicator/ /app/flexCommunicator
RUN pip install --no-cache-dir -e /app/flexCommunicator


# -- this service ---
COPY aggregate_twin/ /app/aggregate_twin

WORKDIR /app/aggregate_twin


ENV TWIN_TICK_HZ=10 \
    TWIN_NAMESPACE=default_ns \
    TWIN_NAME=AggregateTwin \
    PERCEPTION_SOURCE=static

CMD ["python", "main.py"]

ENV PYTHONUNBUFFERED=1
