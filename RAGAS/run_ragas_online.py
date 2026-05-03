

import asyncio
import json
import logging
import os
import sys
import uuid
import re
import traceback
from typing import List, Tuple, Dict, Any

import pandas as pd
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import (
    ContextPrecision, ContextRecall, Faithfulness, ResponseRelevancy
)
from langchain_huggingface import HuggingFaceEmbeddings

# --- パス設定と環境初期化 (重要) ---

# 1. このスクリプトのディレクトリ（RAGAS/）を取得
current_script_dir = os.path.dirname(os.path.abspath(__file__))

# 2. プロジェクトルート（一つ上の階層）を取得
project_root = os.path.dirname(current_script_dir)

# 3. sys.path にルートディレクトリを追加（backend_app.pyなどをインポート可能にするため）
sys.path.append(project_root)

# 4. 作業ディレクトリをルートに変更（backend_app.py内の相対パス読み込みを成功させるため）
os.chdir(project_root)
print(f"Working Directory changed to: {os.getcwd()}")

# --- モジュールインポート ---
try:
    # backend_app.py から必要な関数・変数をインポート
    from backend_app import (
        run_master_agent_flow,  # Agentのメインフロー
        startup_event,          # DBやRAGの初期化関数
        CHAT_SESSIONS,          # チャット履歴管理変数
    )
    # 評価用LLMラッパーのインポート
    from modelscope_wrapper import ModelScopeLLM
except ImportError as e:
    print(f"❌ クリティカルエラー: モジュールのインポートに失敗しました。\n詳細: {e}")
    sys.exit(1)

# --- 定数設定 ---
# データセットはスクリプトと同じディレクトリ内の datasets/test_dataset.json を想定
DATASET_PATH = os.path.join(current_script_dir, "datasets", "test_dataset.json")
OUTPUT_CSV_PATH = os.path.join(current_script_dir, "online_evaluation_result.csv")

# --- ログ設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class AgentAdapter:
    """
    backend_app.py のAgent出力をRagas評価用に変換するアダプタークラス。
    """
    
    @staticmethod
    async def run_query(query: str) -> Tuple[str, List[str]]:
        """
        実際のAgentを実行し、最終回答と使用されたコンテキスト（RAG検索結果）を抽出します。
        
        Args:
            query (str): 評価対象の質問テキスト
            
        Returns:
            Tuple[str, List[str]]: (生成された回答, 検索されたコンテキストのリスト)
        """
        # 評価用にユニークなセッションIDを生成
        session_id = f"eval_{uuid.uuid4()}"
        
        # 1. ジェネレーター（ストリーム）を取得
        # backend_app.py のメインロジックを実行
        agent_generator = run_master_agent_flow(session_id, query)
        
        final_answer = ""
        
        # 2. ストリームを最後まで消費して完了を待つ
        try:
            async for chunk in agent_generator:
                # [TEXT_RESPONSE]タグが含まれるチャンクを回答として結合
                if "[TEXT_RESPONSE]" in chunk:
                    text_part = chunk.replace("data: ", "").replace("[TEXT_RESPONSE]", "").strip()
                    final_answer += text_part
                
                # エラーログの検知
                if "Error:" in chunk or "Exception:" in chunk:
                    logger.warning(f"Agent warning during execution: {chunk}")

        except Exception as e:
            logger.error(f"Agent execution failed: {e}")
            logger.error(traceback.format_exc())
            return "Error during execution", [""]

        # 3. コンテキスト（RAGの検索結果）を抽出
        # CHAT_SESSIONSグローバル変数から、このセッションの履歴を取得
        contexts = []
        session_history = CHAT_SESSIONS.get(session_id, [])
        
        for msg in session_history:
            # role='tool' のメッセージを検索（RAGやDB検索の結果が含まれる）
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                
                # RAGの結果が含まれているか判定（実装依存のキーワードでフィルタリング）
                # 例: "Result", "Found", "Knowledge Base" など
                if "Result" in content or "Found" in content:
                    # ログ用ヘッダーなどのノイズを除去（簡易的な正規表現）
                    clean_content = re.sub(r'【Tool.*?】', '', content).strip()
                    contexts.append(clean_content)

        # コンテキストが空の場合のフォールバック
        if not contexts:
            contexts = [""]

        # もし final_answer が取得できていない場合、履歴の最後のAssistant発言を採用
        if not final_answer:
            last_assistant_msg = next((m for m in reversed(session_history) if m["role"] == "assistant"), None)
            if last_assistant_msg:
                final_answer = last_assistant_msg["content"]
        
        # メモリリーク防止のため履歴を削除
        if session_id in CHAT_SESSIONS:
            del CHAT_SESSIONS[session_id]
            
        return final_answer, contexts


async def run_evaluation():
    """
    評価プロセスのメイン実行関数
    """
    # 1. アプリケーションの初期化
    logger.info("⚙️  システムを初期化中 (startup_event)...")
    try:
        await startup_event()
    except Exception as e:
        logger.error(f"❌ 初期化に失敗しました: {e}")
        return
    
    # 2. データセット読み込み
    logger.info("📂 データセットを読み込んでいます...")
    if not os.path.exists(DATASET_PATH):
        logger.error(f"❌ ファイルが見つかりません: {DATASET_PATH}")
        return

    with open(DATASET_PATH, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)

    questions = []
    ground_truths = []
    answers = []
    contexts = []

    # 3. 推論ループ実行
    logger.info(f"🚀 {len(raw_data)} 件のデータに対して実Agentによる推論を開始します...")
    
    for i, item in enumerate(raw_data):
        q = item.get("question", "")
        gt = item.get("ground_truth", "")
        
        logger.info(f"[{i+1}/{len(raw_data)}] 質問: {q}")
        
        # ★ 実プロジェクトコードの呼び出し ★
        ans, ctx = await AgentAdapter.run_query(q)
        
        questions.append(q)
        ground_truths.append(gt)
        answers.append(ans)
        contexts.append(ctx)
        
        logger.info(f"   -> 回答プレビュー: {ans[:50]}...")
        logger.info(f"   -> 取得コンテキスト数: {len(ctx)}")

    # 4. Ragas 評価実行
    logger.info("⚖️  Ragasスコアリングを実行中...")
    
    dataset = Dataset.from_dict({
        "question": questions,
        "answer": answers,
        "contexts": contexts,
        "ground_truth": ground_truths
    })

    # 評価用モデルの初期化
    judge_llm = ModelScopeLLM()
    embeddings = HuggingFaceEmbeddings(model_name="BAAI/bge-small-zh-v1.5")
    
    metrics = [
        ContextPrecision(),
        ContextRecall(),
        Faithfulness(),
        ResponseRelevancy(),
    ]

    try:
        # 4. Ragas評価実行
        results = evaluate(
            dataset=dataset,
            metrics=metrics,
            llm=judge_llm,
            embeddings=embeddings,
            raise_exceptions=False
        )

        logger.info("📊 評価完了。結果を表示します:")
        print(results)

        # 1. 先にDataFrameに変換する（これは安定して動作します）
        df = results.to_pandas()

        # 2. DataFrameから数値列の平均を計算して、集計用辞書を作成する
        # これにより KeyError: 0 を回避できます
        summary_scores = df.mean(numeric_only=True).to_dict()

        # 集計結果（JSON）の保存
        summary_path = os.path.join(current_script_dir, "evaluation_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            # summary_scores を保存
            json.dump(summary_scores, f, indent=4, ensure_ascii=False)
        logger.info(f"💾 集計スコアを保存しました: {summary_path}")

        # CSVの最終行に平均スコアを追加
        # summary_scores を使って行を作成
        summary_row = summary_scores.copy()
        summary_row['question'] = '=== AVERAGE SCORE ==='
        # 存在しない列を空文字で埋める（エラー防止のため get で取得）
        summary_row['answer'] = ''
        summary_row['contexts'] = ''
        summary_row['ground_truth'] = ''
        
        # 集計行をDataFrameにして結合
        df_summary = pd.DataFrame([summary_row])
        df_final = pd.concat([df, df_summary], ignore_index=True)

        # CSV保存
        df_final.to_csv(OUTPUT_CSV_PATH, index=False, encoding='utf-8-sig')
        logger.info(f"💾 明細および集計を含むCSVを保存しました: {OUTPUT_CSV_PATH}")

    except Exception as e:
        logger.error(f"❌ Ragas評価中にエラーが発生しました: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    # Windows環境でのイベントループポリシー設定
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
    asyncio.run(run_evaluation())