FROM python:3.11-slim AS base


WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p -m 0700 ~/.ssh && ssh-keyscan github.com >> ~/.ssh/known_hosts

# flexComm
RUN --mount=type=ssh,id=custom git clone -b TestBranch-HDT-Project git@github.com:BertVanAcker/flexCommunicator.git /app/flexCommunicator
RUN pip install --no-cache-dir -e /app/flexCommunicator

# core_msgs
RUN --mount=type=ssh,id=custom git clone git@github.com:KbeFr/core-msgs.git /app/core-msgs
RUN pip install --no-cache-dir -e /app/core-msgs


# this service
COPY . /app/aggregate-twin
RUN pip install --no-cache-dir -e /app/aggregate-twin

WORKDIR /app/aggregate-twin

ENV TWIN_TICK_HZ=10 \
    TWIN_NAMESPACE=default_ns \
    TWIN_NAME=AggregateTwin \
    PERCEPTION_SOURCE=static

CMD ["aggregate-twin"]

ENV PYTHONUNBUFFERED=1
