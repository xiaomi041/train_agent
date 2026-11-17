# 火车票路线规划系统 —— 运行镜像
# 注意：fastembed 依赖 onnxruntime，slim 镜像需补 libgomp1，否则 import 会报 libgomp.so.1 缺失。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制全部源码：新增模块（ticket_api.py / watch.py）必须一起进镜像，
# 否则 app 启动后 import 失败而健康检查仍然 200（假的健康）。
# .dockerignore 已排除 .env / .venv / stations.json / test_*.py。
COPY . .

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "streamlit", "run", "a.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
