# 使用官方 Python 运行时作为基础镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制 requirements.txt 并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Node.js 和 Playwright 依赖
RUN apt-get update && \
    apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g playwright && \
    playwright install-deps chromium

# 复制其余的项目代码
COPY . .

# 暴露 FastAPI 默认端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "backend_app:app", "--host", "0.0.0.0", "--port", "8000"]