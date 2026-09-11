FROM python:3.11-slim AS base


WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*


# flexComm
RUN git clone -b TestBranch-HDT-Project https://KbeFr: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@github.com/BertVanAcker/flexCommunicator.git
RUN pip install --no-cache-dir -e /app/flexCommunicator


# core_msgs
RUN git clone https://KbeFr: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@github.com/KbeFr/core-msgs.git
RUN pip install --no-cache-dir -e /app/core-msgs

# Install aggregate_twin requirements
COPY requirements.txt /app/aggregate_twin/requirements.txt
RUN pip install --no-cache-dir -r /app/aggregate_twin/requirements.txt

# -- this service ---
COPY . /app/aggregate_twin

WORKDIR /app/aggregate_twin


ENV TWIN_TICK_HZ=10 \
    TWIN_NAMESPACE=default_ns \
    TWIN_NAME=AggregateTwin \
    PERCEPTION_SOURCE=static

CMD ["python", "main.py"]

ENV PYTHONUNBUFFERED=1
