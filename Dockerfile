# 公式Pythonランタイムをベースイメージとして使用
FROM python:3.11-slim

# ワーキングディレクトリの設定
WORKDIR /app

# requirements.txtをコピーして依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Node.js と Playwright の依存関係をインストール
RUN apt-get update && \
    apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g playwright && \
    playwright install-deps chromium

# 残りのプロジェクトコードをコピー
COPY . .

# FastAPIのデフォルトポートを公開
EXPOSE 8000

# 起動コマンド
CMD ["uvicorn", "backend_app:app", "--host", "0.0.0.0", "--port", "8000"]