<div align="center">

[**🇯🇵 日本語 (Japanese)**](#jp) | [**🇺🇸 English**](#en)

</div>

---

<div id="jp"></div>

# 自然言語駆動型 B2Bターゲット検索 & CRM分析エージェント

## 概要
**「広東省のガラス工場を探して」** という外部検索の要望から、**「環境に配慮している取引先はどこ？」** という曖昧な社内データの問い合わせまで。

本プロジェクトは、営業担当者の自然言語入力を解析し、**外部Web検索（Playwright）** と **社内CRMデータベース（Text-to-SQL & Vector Search）** を使い分ける自律型AIエージェントです。また、高度なRAGパイプライン（ハイブリッド検索 + リランク）を搭載し、長文ドキュメントや非構造化データからも正確に回答を生成します。

単なるデモに留まらず、**Ragasを用いた検索精度のスコアリング**と、**エージェントの判断ロジックを検証するテストパイプライン**を実装し、実用性と信頼性を重視した設計となっています。

## 主な機能と特徴

### 1. ReAct型 自律エージェント (Agentic Decision Making)
ユーザーの意図を分析し、最適なツールを自律的に選択・実行します。
*   **Web Scraper**: Playwrightを用いた動的Webサイトのスクリーニング。
*   **Internal CRM Analyst**: SQLとベクトル検索を使い分け、社内データを多角的に分析。
*   **Knowledge Base (RAG)**: 業界レポートなどの非構造化データを検索。

### 2. Agentic CRM 分析 (Text-to-SQL & Semantic Search)
従来のSQL生成に加え、**pgvector** を用いたセマンティック検索機能を実装しました。
*   **Text-to-SQL**: 「売上10万以上の商談」のような定量的条件を正確なSQLに変換。
*   **Agentic Semantic Search**: 「環境に優しい企業」「物流関連の担当者」といった、SQLでは表現しにくい**曖昧な概念や定性的な特徴**を、ベクトル類似度検索を用いてデータベースから抽出します。

### 3. 高度な RAG パイプライン (Advanced RAG Strategy)
精度と網羅性を両立するため、最新の検索技術を統合しています。
*   **Hybrid Search & Reranking**: **BM25（キーワード検索）** と **Vector Search（意味検索）** を組み合わせ、さらに **Cross-Encoder** によるリランク（順位付け直し）を行うことで、専門用語の検索漏れを防ぎつつ、文脈適合度を高めています。
*   **Long Document Support**: トークン制限を超える長文PDFに対し、ページオフセットを保持したままチャンク化・検索する機能を実装。文脈を維持したまま、該当箇所を正確に引用します。
*   **Agentic Filtering**: 検索結果をそのまま回答に使うのではなく、**「このドキュメントは本当に質問の回答になっているか？」** をLLMが自己評価。ノイズとなるドキュメントを自動的に除外し、ハルシネーション（嘘の生成）を抑制します。

### 4. マルチフォーマット対応 (Azure AI Integration)
Azure AI Document Intelligence と Pandas を組み合わせ、多様な社内ドキュメントをインデックス化しています。
*   **対応フォーマット**: PDF, Word, PowerPoint, 画像 (JPG/PNG), Excel (.xlsx)。
*   **ハイブリッド解析**: 画像化された図表はOCRで、構造化された表データはPandasで解析し、LLMが理解しやすいMarkdownに変換しています。

### 5. 多層的な評価システム (Quantitative Evaluation)
AIアプリケーションの品質を担保するため、**「回答の正確さ」** と **「行動の正しさ」** の両面から評価を行っています。

1.  **RAG精度評価 (Ragas)**: `run_ragas_online.py`
    *   LLM開発で最も重要な「ハルシネーション抑制」を評価します。
    *   **Context Precision（文脈適合率）**: 検索されたドキュメントが質問に関連しているか。
    *   **Faithfulness（忠実度）**: 生成された回答が、検索結果に基づいているか（勝手な創作をしていないか）。
2.  **エージェント判断評価**: `run_agent_eval.py`
    *   30件以上のシナリオに対し、エージェントが適切なツール（SQL vs Vector Search vs Web検索）を選択できたかを回帰テストします。
    *   意図分類の正解率や、抽出パラメータの正確性を検証します。

## 技術スタック

| カテゴリ | 技術・ツール | 用途 |
| --- | --- | --- |
| **Frontend** | React, SSE | チャットUI、ストリーミング表示 |
| **Backend** | Python, FastAPI | 非同期APIサーバー、エージェント制御 |
| **Database** | PostgreSQL (pgvector) | CRMデータ、ベクトル検索 |
| **LLM / AI** | OpenAI SDK (ModelScope/Qwen), Gemini | 推論、SQL生成、RAGフィルタリング |
| **RAG / Search** | LlamaIndex, BM25, SentenceTransformers | ハイブリッド検索、リランク |
| **ETL / Parsing** | Azure Document Intelligence, Pandas | ドキュメント解析（長文・表対応） |
| **Automation** | Playwright | ヘッドレスブラウザ操作 |
| **Evaluation** | Ragas, Custom Script | 精度評価、ロジック評価 |
| **Infra** | Docker, Docker Compose | DB環境の構築 |

## アーキテクチャ図

<img src="./assets/architecture.png" alt="Architecture Diagram" width="500">

## セットアップと実行

### 1. 環境構築

```powershell
# リポジトリのクローン
git clone [repo_url]
cd [repo_name]

# 仮想環境作成
python -m venv venv
.\venv\Scripts\Activate.ps1

# 依存関係インストール
pip install -r requirements.txt
playwright install
```

### 2. データベースの起動 (Docker)

PostgreSQL (pgvector対応) を起動します。

```powershell
docker-compose up -d
```
*   `init_vector_db.sql` により、サンプルデータの投入とベクトル化（Embedding）が自動的に行われます。

### 3. Backend / Frontend の起動

**Backend:**
```powershell
# 環境変数の設定 (api_keys.json または .env)
uvicorn backend_app:app --reload
```

**Frontend:**
```powershell
cd frontend
npm install
npm run dev
```

---

<div id="en"></div>

# Agentic B2B Search & CRM Analysis System

## Overview
**From "Find glass factories in Guangdong" to "Which clients are environmentally friendly?"**

This project is an autonomous AI agent designed to bridge the gap between external market intelligence and internal business data. It interprets natural language requests and intelligently switches between **External Web Scraping (Playwright)** and **Internal CRM Database Analysis (Text-to-SQL & Semantic Search)**. Additionally, it features an advanced RAG pipeline (Hybrid Search + Reranking) to provide accurate answers from long-form documents and unstructured data.

Beyond a simple demo, this project emphasizes reliability by implementing a **quantitative evaluation pipeline using Ragas** for retrieval accuracy and custom regression tests for agent decision logic.

## Key Features

### 1. ReAct Autonomous Agent (Agentic Decision Making)
The agent classifies user intent and executes the appropriate tools autonomously:
*   **Web Scraper**: Screens corporate databases via Playwright automation.
*   **Internal CRM Analyst**: Uses both SQL and Vector Search to analyze internal data.
*   **Knowledge Base (RAG)**: Retrieves unstructured data from industry reports.

### 2. Agentic CRM Analysis (Text-to-SQL & Semantic Search)
In addition to traditional SQL generation, I implemented semantic search using **pgvector**.
*   **Text-to-SQL**: Translates quantitative conditions (e.g., "Deals over 100k") into precise SQL.
*   **Agentic Semantic Search**: Uses vector similarity to extract **abstract concepts or qualitative features** (e.g., "Eco-friendly companies", "Logistics-related contacts") that are difficult to query with standard SQL.

### 3. Advanced RAG Pipeline (Hybrid Search & Agentic Filtering)
Integrated state-of-the-art search technologies to balance precision and recall.
*   **Hybrid Search & Reranking**: Combines **BM25 (Keyword Search)** and **Vector Search (Semantic Search)**, followed by **Cross-Encoder Reranking**. This prevents missing specific technical terms while ensuring high contextual relevance.
*   **Long Document Support**: Implemented a chunking strategy that supports long PDFs beyond token limits while preserving page offsets. It allows precise citation mapping even in lengthy reports.
*   **Agentic Filtering**: The agent acts as a critic. Before generating an answer, the LLM evaluates retrieved documents: **"Does this document actually contain the answer?"** It automatically filters out noise to reduce hallucinations.

### 4. Multi-Format Support (Azure AI Integration)
Leverages Azure AI Document Intelligence and Pandas to index diverse documents.
*   **Formats**: PDF, Word, PowerPoint, Images (JPG/PNG), and **Excel (.xlsx)**.
*   **Hybrid Parsing**: Uses Azure OCR for images/diagrams and Pandas for structured tables, converting everything into LLM-friendly Markdown.

### 5. Multi-layered Evaluation System
To ensure quality in LLM application development, I implemented a dual-layer evaluation process focusing on **"Accuracy of Information"** and **"Correctness of Actions"**.

1.  **RAG Accuracy Evaluation (Ragas)**: `run_ragas_online.py`
    *   This is the primary evaluation to prevent hallucinations.
    *   **Context Precision**: Measures if the retrieved documents are relevant to the query.
    *   **Faithfulness**: Measures if the generated answer is factually consistent with the retrieved context.
2.  **Agent Logic Evaluation**: `run_agent_eval.py`
    *   Regression testing against 30+ scenarios.
    *   Validates if the agent correctly selects between SQL, Vector Search, or Web Search based on the query type.

## Tech Stack

| Category | Technology/Tool | Usage |
| --- | --- | --- |
| **Frontend** | React, SSE | Chat UI, Real-time Streaming |
| **Backend** | Python, FastAPI | Async API Server, Agent Control |
| **Database** | PostgreSQL (pgvector) | CRM Data, Vector Search |
| **LLM / AI** | OpenAI SDK (ModelScope/Qwen), Gemini | Inference, SQL Gen, RAG Filtering |
| **RAG / Search** | LlamaIndex, BM25, SentenceTransformers | Hybrid Search, Reranking |
| **ETL / Parsing** | Azure Document Intelligence, Pandas | Long-doc & Table Parsing |
| **Automation** | Playwright | Headless Browser Automation |
| **Evaluation** | Ragas, Custom Script | RAG Scoring, Logic Testing |
| **Infra** | Docker, Docker Compose | DB Environment |

## Architecture Diagram

<img src="./assets/architecture.png" alt="Architecture Diagram" width="500">

## Setup & Execution

### 1. Environment Setup

```powershell
# Clone Repo
git clone [repo_url]
cd [repo_name]

# Create Venv
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install Dependencies
pip install -r requirements.txt
playwright install
```

### 2. Start Database (Docker)

Start the PostgreSQL container (enabled with pgvector).

```powershell
docker-compose up -d
```
*   `init_vector_db.sql` will automatically populate sample data and generate vector embeddings.

### 3. Start Backend / Frontend

**Backend:**
```powershell
# Ensure API Keys are set
uvicorn backend_app:app --reload
```

**Frontend:**
```powershell
cd frontend
npm install
npm run dev
```