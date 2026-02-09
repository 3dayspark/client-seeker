import json
import os
import sys
import pandas as pd
from datasets import Dataset
from ragas import evaluate
# Ragas評価指標のインポート
from ragas.metrics import (
    ContextPrecision,
    ContextRecall,
    Faithfulness,
    ResponseRelevancy,
)
from langchain_huggingface import HuggingFaceEmbeddings
from modelscope_wrapper import ModelScopeLLM

# --- プロジェクトルートパスの設定（rag_utils読み込み用） ---
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# アプリケーション共通モジュールのインポート
try:
    from rag_utils import build_or_load_index, query_knowledge_base
except ImportError:
    print("❌ rag_utils をインポートできません。パス設定を確認してください。")
    sys.exit(1)

# --- パスおよび定数定義 ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
# テスト用データセット（質問と正解データ）のパス
DATASET_PATH = os.path.join(CURRENT_DIR, "datasets", "test_dataset.json") 
# 評価結果出力先CSVパス
OUTPUT_CSV_PATH = os.path.join(CURRENT_DIR, "online_evaluation_result.csv")

def run_inference_and_evaluate():
    """
    RAGシステムの推論を実行し、Ragasを用いて評価を行うメイン関数
    """
    print("🚀 オンライン評価システムを起動しています...")

    # 1. RAGエンジンの初期化（インデックスのロード）
    print("🔄 ベクトルデータベースのインデックスを読み込んでいます...")
    index = build_or_load_index()
    if not index:
        print("❌ インデックスの読み込みに失敗しました。先にメインプログラムを実行してインデックスを生成してください。")
        return

    # 2. テストデータセットの読み込み
    print("📂 テストデータセットを読み込んでいます...")
    if not os.path.exists(DATASET_PATH):
        print(f"❌ ファイルが見つかりません: {DATASET_PATH}")
        return

    with open(DATASET_PATH, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)

    # データ格納用リストの初期化
    questions = []
    ground_truths = []
    answers = []
    contexts = []

    # 3. リアルタイム推論の実行ループ
    print(f"⚡ {len(raw_data)} 件の質問に対してリアルタイム推論を開始します...")
    
    for i, item in enumerate(raw_data):
        q = item["question"]
        gt = item["ground_truth"]
        
        # 進捗表示
        print(f"   [{i+1}/{len(raw_data)}] 質問: {q[:20]}...")
        
        # RAG検索および回答生成の実行
        try:
            # query_knowledge_baseは (回答, 引用ファイルリスト, 検索されたコンテキスト) を返す
            response, _, retrieved_chunk_texts = query_knowledge_base(index, q)
            
            # 結果をリストに格納
            questions.append(q)
            ground_truths.append(gt)
            answers.append(response)
            
            # Ragasの仕様に合わせてコンテキスト形式を調整 (list[str])
            # 検索結果が空の場合は空文字を設定してエラーを回避
            if not retrieved_chunk_texts:
                contexts.append([""])
            else:
                contexts.append(retrieved_chunk_texts)
            
        except Exception as e:
            print(f"   ❌ 推論エラー: {e}")
            questions.append(q)
            ground_truths.append(gt)
            answers.append("Error processing request")
            contexts.append(["Error"])

    # 4. 評価用データセットの構築
    data_dict = {
        "question": questions,
        "answer": answers,
        "contexts": contexts,
        "ground_truth": ground_truths
    }
    ragas_dataset = Dataset.from_dict(data_dict)

    # 5. 評価実行（ModelScope/Qwenを利用）
    print("⚖️  推論完了。RAGASによるスコアリングを開始します...")
    
    judge_llm = ModelScopeLLM()
    embeddings = HuggingFaceEmbeddings(model_name="BAAI/bge-m3")

    # 評価指標の定義
    metrics_list = [
        ContextPrecision(),
        ContextRecall(),
        Faithfulness(),
        ResponseRelevancy(),
    ]
    
    # 実行設定のロード
    from ragas.run_config import RunConfig
    
    try:
        # 評価プロセスの実行
        # APIレート制限回避のため、同時実行数を1に制限
        results = evaluate(
            dataset=ragas_dataset,
            metrics=metrics_list,
            llm=judge_llm,
            embeddings=embeddings,
            run_config=RunConfig(max_workers=1, timeout=120) 
        )

        # 6. 評価結果の保存
        print("\n📊 評価結果:")
        print(results)
        
        df = results.to_pandas()
        df.to_csv(OUTPUT_CSV_PATH, index=False, encoding='utf-8-sig')
        print(f"\n💾 結果を保存しました: {OUTPUT_CSV_PATH}")

    except Exception as e:
        print(f"❌ 評価プロセス中にエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_inference_and_evaluate()