# ステージ1: 依存関係のインストール（キャッシュ可能）
FROM python:3.11-slim as deps
WORKDIR /app

# システム依存関係と Playwright のインストール
RUN apt-get update && \
    apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g playwright && \
    playwright install-deps chromium && \
    rm -rf /var/lib/apt/lists/*

# Python 依存ライブラリのコピーとインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip cache purge

# ステージ2: 最終イメージのビルド
FROM python:3.11-slim
WORKDIR /app

# ステージ1からインストール済みの依存関係をコピー
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# プロジェクトの残りのコードをコピー
COPY . .

# ポートを公開
EXPOSE 8000

# 起動コマンド
CMD ["uvicorn", "backend_app:app", "--host", "0.0.0.0", "--port", "8000"]