import os
import json
import logging
from typing import Optional, Tuple, List, Any
import pandas as pd

# --- Azure SDK ---
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

# --- LlamaIndex Core ---
from llama_index.core import (
    VectorStoreIndex,
    SimpleDirectoryReader,
    StorageContext,
    load_index_from_storage,
    Document,
    Settings,
    QueryBundle
)
from llama_index.core.schema import NodeWithScore
from llama_index.core.retrievers import VectorIndexRetriever
from llama_index.core.postprocessor.types import BaseNodePostprocessor

# --- Hybrid Search & Embedding ---
from llama_index.retrievers.bm25 import BM25Retriever
# エラー回避のため、FusionModeのインポートは行わず、クラスのみインポート
from llama_index.core.retrievers import QueryFusionRetriever

# --- Reranking用 (sentence-transformers直接利用) ---
from sentence_transformers import CrossEncoder
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ロガー設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Azureログ抑制
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# --- 定数設定 ---
DOCS_DIR = "knowledge_docs"
# ユーザー指定のパスに修正
LONG_DOC_OUTPUT_DIR = os.path.join("long_doc_process", "data_output") 
PERSIST_DIR = "storage_index"
EMBED_MODEL_NAME = "BAAI/bge-m3"
RERANK_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2" 
API_KEYS_FILE = "api_keys.json"

# --- グローバル設定 ---
Settings.llm = None
Settings.embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)


# ==========================================
# 1. カスタムコンポーネント定義
# ==========================================

class LocalJSONChunkReader:
    """
    pdf_processor.py で生成されたJSONチャンクファイルを読み込むリーダー
    """
    def load_data(self, output_dir: str) -> List[Document]:
        documents = []
        if not os.path.exists(output_dir):
            logger.warning(f"JSON出力ディレクトリが見つかりません: {output_dir}")
            return []

        files = [f for f in os.listdir(output_dir) if f.endswith("_chunks.json")]
        logger.info(f"📂 長文ドキュメントのチャンクJSONを {len(files)} 件検出しました。")

        for filename in files:
            file_path = os.path.join(output_dir, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    chunks = json.load(f)
                
                for chunk in chunks:
                    text = chunk.get("text", "")
                    metadata = chunk.get("metadata", {})
                    
                    # LlamaIndexのDocumentオブジェクトに変換
                    doc = Document(
                        text=text,
                        metadata=metadata,
                        excluded_llm_metadata_keys=["file_path"], 
                        excluded_embed_metadata_keys=["page_numbers"] 
                    )
                    documents.append(doc)
            except Exception as e:
                logger.error(f"JSON読み込みエラー {file_path}: {e}")
        
        return documents

class LocalSentenceTransformerRerank(BaseNodePostprocessor):
    """
    sentence-transformersを直接使用するカスタムリランカー
    """
    model: Any = None
    top_n: int = 3

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2", top_n: int = 3):
        super().__init__()
        try:
            # CrossEncoderの初期化
            self.model = CrossEncoder(model_name)
            self.top_n = top_n
            logger.info(f"Rerankモデル {model_name} をロードしました。")
        except Exception as e:
            logger.error(f"Rerankモデルのロードに失敗: {e}")
            raise e

    def _postprocess_nodes(
        self,
        nodes: List[NodeWithScore],
        query_bundle: Optional[QueryBundle] = None,
    ) -> List[NodeWithScore]:
        if not nodes:
            return []
        
        query_text = query_bundle.query_str
        pairs = [(query_text, node.node.get_content()) for node in nodes]
        
        # スコアリング実行
        scores = self.model.predict(pairs)
        
        for node, score in zip(nodes, scores):
            node.score = float(score)

        sorted_nodes = sorted(nodes, key=lambda x: x.score, reverse=True)
        return sorted_nodes[:self.top_n]

class LocalExcelReader:
    def load_data(self, file_path: str, extra_info: Optional[dict] = None) -> List[Document]:
        try:
            dfs = pd.read_excel(file_path, sheet_name=None)
            full_text = ""
            for sheet_name, df in dfs.items():
                full_text += f"\n## Sheet: {sheet_name}\n"
                markdown_table = df.fillna("").to_markdown(index=False)
                full_text += markdown_table + "\n"
            return [Document(text=full_text, metadata={"file_name": os.path.basename(file_path)})]
        except Exception as e:
            logger.error(f"Excel解析エラー {file_path}: {e}")
            return []

class LocalAzureReader:
    def __init__(self, api_endpoint: str, api_key: str):
        self.client = DocumentIntelligenceClient(
            endpoint=api_endpoint, 
            credential=AzureKeyCredential(api_key)
        )

    def load_data(self, file_path: str, extra_info: Optional[dict] = None) -> List[Document]:
            try:
                full_name = os.path.relpath(file_path, DOCS_DIR)
                with open(file_path, "rb") as f:
                    poller = self.client.begin_analyze_document(
                        model_id="prebuilt-layout", 
                        body=f,  
                        output_content_format="markdown"
                    )
                result = poller.result()
                return [Document(text=result.content, metadata={"file_name": full_name})]
            except Exception as e:
                logger.error(f"Azure解析エラー {file_path}: {e}")
                return []

def load_azure_config():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(current_dir, API_KEYS_FILE)
    if not os.path.exists(json_path): return None, None
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get("azure", {}).get("endpoint"), data.get("azure", {}).get("key")
    except: return None, None


# ==========================================
# 2. インデックス構築と検索ロジック
# ==========================================

def build_or_load_index() -> Optional[VectorStoreIndex]:
    """
    Knowledge Docs (Azure/Excel) と Long Doc (JSON) を統合してインデックス化
    """
    # ★重要: 新しいパス設定などを反映させるため、storage_indexフォルダを一度削除することを推奨します
    if not os.path.exists(PERSIST_DIR):
        logger.info("🆕 新規インデックス構築を開始します...")
        
        all_documents = []

        # 1. 既存 Knowledge Docs の読み込み
        azure_endpoint, azure_key = load_azure_config()
        if azure_endpoint and azure_key and os.path.exists(DOCS_DIR):
            azure_reader = LocalAzureReader(azure_endpoint, azure_key)
            excel_reader = LocalExcelReader()
            
            file_extractor = {
                ".pdf": azure_reader, ".xlsx": excel_reader, 
                ".docx": azure_reader, ".pptx": azure_reader,
                ".jpg": azure_reader, ".png": azure_reader
            }
            
            try:
                raw_docs = SimpleDirectoryReader(
                    DOCS_DIR, 
                    file_extractor=file_extractor,
                    recursive=True
                ).load_data()
                all_documents.extend(raw_docs)
                logger.info(f"✅ Knowledge Docs: {len(raw_docs)} 件ロード完了")
            except Exception as e:
                logger.error(f"Knowledge Docs ロード失敗: {e}")

        # 2. Long Doc (pdf_processor output) の読み込み
        json_reader = LocalJSONChunkReader()
        json_docs = json_reader.load_data(LONG_DOC_OUTPUT_DIR)
        all_documents.extend(json_docs)
        logger.info(f"✅ Long Docs (JSON): {len(json_docs)} チャンクロード完了")

        if not all_documents:
            logger.error("❌ ドキュメントが1件も読み込めませんでした。")
            return None

        try:
            # インデックス構築
            index = VectorStoreIndex.from_documents(all_documents)
            index.storage_context.persist(persist_dir=PERSIST_DIR)
            logger.info(f"💾 インデックスを保存しました: {PERSIST_DIR}")
            return index
        except Exception as e:
            logger.error(f"インデックス構築エラー: {e}")
            return None

    else:
        logger.info("🔄 既存のインデックスをロード中...")
        try:
            storage_context = StorageContext.from_defaults(persist_dir=PERSIST_DIR)
            index = load_index_from_storage(storage_context)
            return index
        except Exception as e:
            logger.error(f"インデックスロードエラー: {e}")
            return None

def query_knowledge_base(
    index: Optional[VectorStoreIndex], 
    query_text: str, 
    top_k_retrieval: int = 10, 
    top_k_final: int = 3,      
    bm25_weight: float = 0.5   
) -> Tuple[str, List[str]]:
    """
    Hybrid Search (Simple Fusion) + Reranking
    """
    if index is None:
        return "（ナレッジベース未初期化）", []

    try:
        # 1. 各Retrieverの準備
        vector_retriever = VectorIndexRetriever(
            index=index,
            similarity_top_k=top_k_retrieval
        )
        
        bm25_retriever = BM25Retriever.from_defaults(
            docstore=index.docstore,
            similarity_top_k=top_k_retrieval
        )

        # 2. Hybrid Search の実行
        # ★修正ポイント: mode指定を削除し、デフォルト（Simple Fusion）を使用する
        # これにより "Invalid fusion mode" エラーを確実に回避します。
        fusion_retriever = QueryFusionRetriever(
            [vector_retriever, bm25_retriever],
            similarity_top_k=top_k_retrieval,
            num_queries=1, 
            use_async=False
            # mode引数は削除 (デフォルト動作)
        )

        nodes = fusion_retriever.retrieve(query_text)

        # 3. Reranking (Cross-Encoder) の実行
        reranker = LocalSentenceTransformerRerank(
            model_name=RERANK_MODEL_NAME,
            top_n=top_k_final
        )
        
        reranked_nodes = reranker.postprocess_nodes(
            nodes, 
            query_bundle=QueryBundle(query_text)
        )

        if not reranked_nodes:
            return "（関連ドキュメントなし）", []

        # for RAGAS
        retrieved_contexts = [node.node.get_content() for node in reranked_nodes]
        
        # 4. 結果の整形（引用メタデータ付き）
        result_text = ""
        hit_files = []

        for i, node_with_score in enumerate(reranked_nodes):
            node = node_with_score.node
            score = node_with_score.score
            meta = node.metadata
            
            file_name = meta.get('file_name', 'unknown')
            page_nums = meta.get('page_numbers', []) 
            section = meta.get('section_title', '')

            if page_nums:
                pages_str = ", ".join(map(str, page_nums))
                citation_tag = f"[{file_name} p.{pages_str}]"
            else:
                citation_tag = f"[{file_name}]"

            header_info = f" (Section: {section})" if section else ""
            content_snippet = node.get_content().strip()
            
            result_text += f"\n--- Reference {i+1} {citation_tag}{header_info} (Confidence: {score:.4f}) ---\n"
            result_text += f"{content_snippet}\n"

            if file_name not in hit_files:
                hit_files.append(file_name)

        return result_text, hit_files, retrieved_contexts

    except Exception as e:
        logger.error(f"検索プロセスで例外発生: {e}", exc_info=True)
        return f"検索エラー: {e}", []

def get_all_indexed_filenames(index: Optional[VectorStoreIndex]) -> set:
    """
    インデックスに登録されているすべてのファイル名を取得し、Setとして返します。
    ハルシネーション（存在しないファイルの引用）検知用です。
    """
    if index is None:
        return set()
    
    try:
        # docstoreから全ドキュメントのメタデータを走査
        # 注意: ドキュメント数が多い場合、キャッシュ戦略が必要ですが、
        # 数千件程度ならこの方法で高速に取得可能です。
        all_docs = index.docstore.docs.values()
        filenames = set()
        
        for doc in all_docs:
            if hasattr(doc, 'metadata'):
                fname = doc.metadata.get('file_name')
                if fname:
                    filenames.add(fname)
        
        return filenames
    except Exception as e:
        logger.error(f"ファイル名リスト取得エラー: {e}")
        return set()


if __name__ == "__main__":
    # テスト実行
    idx = build_or_load_index()
    if idx:
        res, files = query_knowledge_base(idx, "水素燃料電池のサプライチェーン")
        print(res)