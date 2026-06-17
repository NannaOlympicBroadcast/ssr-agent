#!/bin/bash
set -e

# Setup proxy if passed
if [ -n "$HTTP_PROXY" ]; then
    export http_proxy="$HTTP_PROXY"
    export https_proxy="$HTTPS_PROXY"
    export no_proxy="localhost,127.0.0.1,host.docker.internal"
fi

echo "Installing system dependencies..."
apt-get update && apt-get install -y git

echo "Installing python dependencies from Aliyun PyPI mirror..."
pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ evalscope swebench==4.1.0

echo "Installing ssr-agent in editable mode..."
pip install -e .

echo "Running benchmark..."
python run_benchmark.py
