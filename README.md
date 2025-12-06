# 対話型・企業データベース検索AI (Agentic RAG / Playwright)

## 概要
**「広東省にある、資本金1億以上の自動車ガラスメーカーを探したい」**

営業担当者が入力するこのような抽象的な自然言語の要望を解析し、複雑な企業データベースサイト（SaaS/検索ポータル）の「詳細検索フォーム」へ自動的に条件をマッピング・入力・実行する自律型AIエージェントです。

従来のルールベースのRPAとは異なり、LLM（大規模言語モデル）を用いたAgentic RAG（自律的検索拡張生成）アーキテクチャを採用することで、未知の検索条件や揺らぎのある表現にも柔軟に対応します。

## 主な機能と特徴

### 1. 自律的な意思決定 (ReAct Agent Pattern)
バックエンド（`backend_app.py`）では、ユーザーの入力をそのまま検索に使うのではなく、エージェントが「思考・行動・観察」のループを実行します。
*   **思考**: ユーザーの要望に対し、情報が不足していないか判断。
*   **行動**: 知識不足なら「社内ナレッジベース(RAG)」を検索、情報が揃えば「ブラウザ操作ツール」を実行、不明点があれば「ユーザーに逆質問」を行います。

### 2. 高度なDOM解析とコスト削減 (LLM x Playwright)
Webページ全体を単純にLLMに渡すのではなく、Playwrightを用いてDOM構造を解析し、**「意味のある選択肢（チェックボックスやドロップダウン）」のみを抽出して軽量なJSON形式**でLLMに提示します（`playwright_test.py`）。
*   **効果**: 画像認識や全文解析に比べ、**トークン消費量を約90%削減**しつつ、推論精度を向上させました。
*   **複雑なUI対応**: 深い階層構造を持つ「業界分類ツリー」も、DFS（深さ優先探索）アルゴリズムで自動展開し、最適なカテゴリを特定します。

### 3. リアルタイム・ストリーミングUI
ReactフロントエンドとFastAPIバックエンドをSSE (Server-Sent Events) で接続。
*   AIの「思考プロセス（Thinking...）」
*   ブラウザ操作の「リアルタイムスクリーンショット」
*   操作完了後の「実行レポート」
をチャット形式で可視化し、ユーザーに安心感を与えるUXを実現しました。

## 技術スタック

| カテゴリ | 技術・ツール | 用途 |
| --- | --- | --- |
| **Frontend** | React, CSS (Custom) | チャットUI、ログ可視化、SSE受信 |
| **Backend** | Python, FastAPI | 非同期APIサーバー、エージェント制御 |
| **LLM / AI** | OpenAI SDK (ModelScope/Qwen), Gemini API | 推論、コード生成、JSON解析 |
| **RAG** | LlamaIndex, HuggingFace Embeddings | 業界知識（サプライチェーン等）の検索 |
| **Automation** | Playwright (Async API) | ヘッドレスブラウザ操作、DOM解析 |
| **Infra/Others** | SSE (Server-Sent Events) | ストリーミング通信 |

## アーキテクチャ図

<img src="./assets/architecture.png" alt="Architecture Diagram" width="500">

## プロジェクト構造 (主要ファイル抜粋)

```text
.
├── backend_app.py       # FastAPIアプリケーションエントリポイント (ReAct Agent実装)
├── playwright_test.py   # ブラウザ操作ロジック (LLM x Playwright連携)
├── rag_utils.py         # LlamaIndexを用いたナレッジベース検索ロジック
├── requirements.txt     # バックエンド用依存ライブラリ
├── knowledge_docs/      # RAG用の業界知識ドキュメント格納フォルダ
└── frontend/            # フロントエンドプロジェクト
    └── src/
        ├── App.jsx      # メインのチャットUIとログ表示コンポーネント
        ├── App.css      # チャット画面のスタイリング
        ├── main.jsx     # Reactのエントリポイント
        └── assets/      # 静的リソースフォルダ
```

## 工夫した点（技術的ハイライト）

### 排他制御と論理的推論の組み合わせ
業界分類ツリーの選択において、「親カテゴリ」と「子カテゴリ」が同時に選択された場合、より具体的な「子カテゴリ」を優先して親の選択を解除する**排他制御ロジック**をPython側で実装し、検索ノイズを減らしました。

### エラーハンドリングと自己修復
LLMが生成するJSON形式が崩れていた場合、正規表現を用いて自動修復するロジック（`extract_json_from_text`）を実装し、システムの実用的な安定性を高めています。

### ハイブリッドLLM構成
推論コストと精度のバランスを取るため、メインの推論には「Gemini Flash」、サブタスクやバックアップには「Qwen (ModelScope)」を切り替えて使用できる設計にしています。


## セットアップと実行

本プロジェクトは、Backend（Python/FastAPI）とFrontend（React）を別々のターミナルで起動して連携させます。

### 1. 環境構築 (初回のみ)

プロジェクトのルートディレクトリで以下のコマンドを実行し、仮想環境を作成・有効化した後、依存ライブラリをインストールします。

**Windows (PowerShell)**
```powershell
# 仮想環境の作成
python -m venv venv

# 仮想環境の有効化
.\venv\Scripts\Activate.ps1

# バックエンド依存ライブラリのインストール
pip install -r requirements.txt

# Playwright用ブラウザバイナリのダウンロード（必須）
playwright install
```

### 2. Backend の起動

仮想環境が有効な状態で、以下のコマンドを実行してAPIサーバーを立ち上げます。

```powershell
# 仮想環境が未有効の場合は先に実行: .\venv\Scripts\Activate.ps1

# ローカル開発用（自分だけがアクセスする場合）
uvicorn backend_app:app --reload --port 8000

# 【オプション】同一LAN内のスマホ等からアクセスする場合
# uvicorn backend_app:app --reload --host 0.0.0.0 --port 8000
```
*   起動成功後、コンソールに `Uvicorn running on ...` と表示されます。

### 3. Frontend の起動

別のターミナルを開き、フロントエンドディレクトリへ移動して起動します。

```powershell
cd frontend

# 初回のみ依存パッケージをインストール
npm install

# 開発サーバーを起動
npm run dev
```
*   ブラウザで `http://localhost:5173`（または表示されたURL）にアクセスしてチャット画面を開きます。
*   バックエンドをLAN公開モード（`0.0.0.0`）で起動した場合、スマホからは `http://[PCのIPアドレス]:5173` でアクセスしてください（※Viteの設定で `--host` が必要な場合があります）。



