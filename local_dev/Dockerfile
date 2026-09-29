FROM python:3.11-slim AS base


WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install flexCommunicator requirements
COPY flexCommunicator/requirements.txt /app/flexCommunicator/requirements.txt
RUN pip install --no-cache-dir -r /app/flexCommunicator/requirements.txt

# ---  install local editable packages
COPY core_msgs/ /app/core_msgs
RUN pip install --no-cache-dir -e /app/core_msgs

COPY flexCommunicator/ /app/flexCommunicator
RUN pip install --no-cache-dir -e /app/flexCommunicator


# this service
COPY aggregate-twin/ /app/aggregate-twin
RUN pip install --no-cache-dir -e /app/aggregate-twin

WORKDIR /app/aggregate-twin

ENV TWIN_TICK_HZ=10 \
    TWIN_NAMESPACE=default_ns \
    TWIN_NAME=AggregateTwin \
    PERCEPTION_SOURCE=static

CMD ["aggregate-twin"]

ENV PYTHONUNBUFFERED=1
