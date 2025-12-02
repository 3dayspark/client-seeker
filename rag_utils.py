
import os
import logging
from typing import Optional

from llama_index.core import (
    VectorStoreIndex,
    SimpleDirectoryReader,
    StorageContext,
    load_index_from_storage,
    Settings
)
from llama_index.core.base.base_query_engine import BaseQueryEngine
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ロガーの設定
logger = logging.getLogger(__name__)

# --- 設定定数 ---
DOCS_DIR = "knowledge_docs"
PERSIST_DIR = "storage_index"  # インデックスの永続化ディレクトリ
EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"  # 採用するEmbeddingモデル

# --- グローバル設定 ---
# ローカルEmbeddingを強制使用し、LLM（OpenAI等）は無効化する（検索機能のみ使用するため）
Settings.llm = None
Settings.embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)


def build_or_load_index() -> Optional[VectorStoreIndex]:
    """
    インデックスを初期化します。
    ローカルに保存されたインデックスが存在すればロードし、
    存在しなければドキュメントディレクトリから新規に構築します。

    Returns:
        VectorStoreIndex or None: 構築またはロードされたインデックス。失敗時はNone。
    """
    if not os.path.exists(PERSIST_DIR):
        logger.info("ローカルインデックスが見つかりません。%s から構築を開始します...", DOCS_DIR)
        
        if not os.path.exists(DOCS_DIR):
            os.makedirs(DOCS_DIR)
            logger.warning("ドキュメントディレクトリが存在しません。%s にドキュメントを配置してください。", DOCS_DIR)
            return None

        # 指定ディレクトリ配下の全サポートファイルを読み込み
        documents = SimpleDirectoryReader(DOCS_DIR).load_data()
        if not documents:
            logger.warning("ディレクトリが空のため、インデックス構築をスキップします。")
            return None

        try:
            index = VectorStoreIndex.from_documents(documents)
            index.storage_context.persist(persist_dir=PERSIST_DIR)
            logger.info("インデックスの構築と保存が完了しました。")
            return index
        except Exception as e:
            logger.error("インデックス構築中にエラーが発生しました: %s", e)
            return None
    else:
        logger.info("ローカルインデックスをロードしています...")
        try:
            storage_context = StorageContext.from_defaults(persist_dir=PERSIST_DIR)
            index = load_index_from_storage(storage_context)
            logger.info("インデックスのロードに成功しました。")
            return index
        except Exception as e:
            logger.error("インデックスのロードに失敗しました: %s", e)
            return None


def query_knowledge_base(
    index: Optional[VectorStoreIndex], 
    query_text: str, 
    k: int = 3, 
    score_threshold: float = 0.5
) -> str:
    """
    ナレッジベースからクエリに関連する情報を検索します。
    類似度スコアが閾値（score_threshold）未満の結果は除外されます。

    Args:
        index (VectorStoreIndex): 検索対象のインデックス
        query_text (str): 検索クエリ
        k (int): 最大取得件数
        score_threshold (float): 類似度の下限閾値

    Returns:
        str: 整形された検索結果テキスト
    """
    if index is None:
        logger.warning("インデックスがNoneのため、検索を実行できません。")
        return "（ナレッジベースが初期化されていないか、空です）"

    try:
        # Top-k検索の実行
        retriever = index.as_retriever(similarity_top_k=k)
        nodes = retriever.retrieve(query_text)
        
        valid_nodes = []
        for node in nodes:
            # デバッグ用ログ: スコアを確認し、閾値調整の参考にする
            # 関連ドキュメントが除外される場合は閾値を下げ、無関係なものが含まれる場合は上げてください
            logger.debug("File: %s | Score: %.4f", node.metadata.get('file_name'), node.score)
            
            # 閾値によるフィルタリング (LlamaIndex + BGEではスコアが高いほど類似度が高い)
            if node.score >= score_threshold:
                valid_nodes.append(node)
        
        if not valid_nodes:
            logger.info("閾値(%.2f)を超える関連ドキュメントが見つかりませんでした。", score_threshold)
            return "（関連するドキュメントが見つかりませんでした。関連度がしきい値を下回っています。）"

        result_text = ""
        for i, node in enumerate(valid_nodes):
            file_name = node.metadata.get('file_name', 'unknown')
            result_text += f"\n--- 参考ドキュメント {i+1} (ソース: {file_name}) ---\n{node.text}\n"
            
        return result_text

    except Exception as e:
        logger.error("検索処理中に例外が発生しました: %s", e)
        return f"検索エラー: {e}"