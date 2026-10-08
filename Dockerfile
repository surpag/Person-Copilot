# 使用官方轻量级 Python 镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（某些 Python 包可能需要）
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装（利用 Docker 缓存层，加速构建）
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制项目所有代码
COPY . .

# 创建数据目录（用于挂载持久化卷）
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 8000

# 启动命令：使用 uvicorn 运行 FastAPI 应用
CMD ["uvicorn", "backend.agent.api.main:app", "--host", "0.0.0.0", "--port", "8000"]