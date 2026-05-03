import os
import json
import logging
from typing import Optional, Tuple, List, Any, Set
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
from llama_index.core.schema import NodeWithScore, BaseNode
# メタデータフィルタリング用
from llama_index.core.vector_stores import MetadataFilters, ExactMatchFilter
from llama_index.core.retrievers import VectorIndexRetriever, BaseRetriever
from llama_index.core.postprocessor.types import BaseNodePostprocessor

# --- Hybrid Search & Embedding ---
import jieba
from transformers import AutoTokenizer
# from llama_index.retrievers.bm25 import BM25Retriever # <-- 削除またはコメントアウト
from llama_index.core.retrievers import QueryFusionRetriever

# --- Rank BM25 ---
from rank_bm25 import BM25Okapi

# --- Reranking用 ---
from sentence_transformers import CrossEncoder
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# --- ログ設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# --- 定数設定 ---
DOCS_DIR = "knowledge_docs"
LONG_DOC_OUTPUT_DIR = os.path.join("long_doc_process", "data_output")
PERSIST_DIR = "storage_index"
EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
RERANK_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
API_KEYS_FILE = "api_keys.json"

# --- グローバル設定 ---
Settings.llm = None
Settings.embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)

_BERT_TOKENIZER = AutoTokenizer.from_pretrained("BAAI/bge-small-zh-v1.5")


# ==========================================
# 0. デバッグ用ヘルパー関数
# ==========================================
def _log_debug_nodes(stage_name: str, nodes: List[NodeWithScore], top_n: int = 3):
    logger.info(f"\n🔍 --- [DEBUG: {stage_name}] Top {top_n} ---")
    if not nodes:
        logger.info("   (結果なし)")
        return

    for i, node in enumerate(nodes[:top_n]):
        meta = node.node.metadata
        file_name = meta.get('file_name', 'unknown')
        doc_type = meta.get('doc_type', 'unknown')
        score = node.score if node.score is not None else 0.0
        
        content_preview = node.node.get_content().replace('\n', ' ')[:50] + "..."
        
        logger.info(
            f"   #{i+1} [Score: {score:.4f}] "
            f"File: {file_name} ({doc_type}) | Text: {content_preview}"
        )
    logger.info("------------------------------------------------")


# ==========================================
# 1. カスタムコンポーネント定義
# ==========================================

# ... (Readerクラスなどは変更なしのため省略) ...
class LocalJSONChunkReader:
    def load_data(self, output_dir: str) -> List[Document]:
        documents = []
        if not os.path.exists(output_dir):
            return []
        files = [f for f in os.listdir(output_dir) if f.endswith("_chunks.json")]
        for filename in files:
            file_path = os.path.join(output_dir, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    chunks = json.load(f)
                for chunk in chunks:
                    text = chunk.get("text", "")
                    metadata = chunk.get("metadata", {})
                    metadata["doc_type"] = "long_doc"
                    doc = Document(text=text, metadata=metadata, excluded_llm_metadata_keys=["file_path", "doc_type"], excluded_embed_metadata_keys=["page_numbers", "doc_type"])
                    documents.append(doc)
            except Exception: pass
        return documents

class LocalSentenceTransformerRerank(BaseNodePostprocessor):
    model: Any = None
    top_n: int = 3
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2", top_n: int = 3):
        super().__init__()
        self.model = CrossEncoder(model_name)
        self.top_n = top_n
    def _postprocess_nodes(self, nodes: List[NodeWithScore], query_bundle: Optional[QueryBundle] = None) -> List[NodeWithScore]:
        if not nodes: return []
        query_text = query_bundle.query_str
        pairs = [(query_text, node.node.get_content()) for node in nodes]
        scores = self.model.predict(pairs)
        for node, score in zip(nodes, scores):
            node.score = float(score)
        return sorted(nodes, key=lambda x: x.score, reverse=True)[:self.top_n]

class LocalExcelReader:
    def load_data(self, file_path: str, extra_info: Optional[dict] = None) -> List[Document]:
        try:
            dfs = pd.read_excel(file_path, sheet_name=None)
            full_text = ""
            for sheet_name, df in dfs.items():
                full_text += f"\n## Sheet: {sheet_name}\n"
                full_text += df.fillna("").to_markdown(index=False) + "\n"
            return [Document(text=full_text, metadata={"file_name": os.path.basename(file_path), "doc_type": "short_doc"})]
        except: return []

class LocalTextReader:
    """
    シンプルなテキストファイル (.txt, .md等) 用リーダー
    """
    def load_data(self, file_path: str, extra_info: Optional[dict] = None) -> List[Document]:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            return [Document(
                text=content, 
                metadata={
                    "file_name": os.path.basename(file_path),
                    "doc_type": "short_doc"  # これが必須
                }
            )]
        except Exception as e:
            logger.error(f"Text解析エラー {file_path}: {e}")
            return []

class LocalAzureReader:
    def __init__(self, api_endpoint: str, api_key: str):
        self.client = DocumentIntelligenceClient(endpoint=api_endpoint, credential=AzureKeyCredential(api_key))
    def load_data(self, file_path: str, extra_info: Optional[dict] = None) -> List[Document]:
        try:
            full_name = os.path.relpath(file_path, DOCS_DIR)
            with open(file_path, "rb") as f:
                poller = self.client.begin_analyze_document(model_id="prebuilt-layout", body=f, output_content_format="markdown")
            result = poller.result()
            return [Document(text=result.content, metadata={"file_name": full_name, "doc_type": "short_doc"})]
        except: return []

def load_azure_config():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(current_dir, API_KEYS_FILE)
    if not os.path.exists(json_path): return None, None
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get("azure", {}).get("endpoint"), data.get("azure", {}).get("key")
    except: return None, None


# --- カスタムBM25 Retriever★ ---
class LocalBM25Retriever(BaseRetriever):
    """
    rank_bm25ライブラリを使用した、完全制御可能なBM25 Retriever
    """
    def __init__(
        self,
        nodes: List[BaseNode],
        tokenizer,
        similarity_top_k: int = 10,
    ) -> None:
        super().__init__()
        self.nodes = nodes
        self.tokenizer = tokenizer
        self.similarity_top_k = similarity_top_k
        
        # インデックス構築（ここでトークナイズを実行）
        # コンテンツ全体をトークナイズ
        self.corpus_tokens = [tokenizer(node.get_content()) for node in nodes]
        self.bm25 = BM25Okapi(self.corpus_tokens)

    def _retrieve(self, query_bundle: QueryBundle) -> List[NodeWithScore]:
        query_text = query_bundle.query_str
        # クエリをトークナイズ
        tokenized_query = self.tokenizer(query_text)
        
        # スコア計算
        scores = self.bm25.get_scores(tokenized_query)
        
        # スコア順にソートして上位を取得
        # (index, score) のペアを作成
        indices_with_scores = enumerate(scores)
        sorted_indices = sorted(indices_with_scores, key=lambda x: x[1], reverse=True)
        
        top_indices = sorted_indices[:self.similarity_top_k]
        
        result_nodes = []
        for idx, score in top_indices:
            # スコアが0より大きいものだけ返す（オプション）
            if score > 0:
                node = self.nodes[idx]
                result_nodes.append(NodeWithScore(node=node, score=float(score)))
                
        return result_nodes

# ==========================================
# 2. インデックス構築と検索ロジック
# ==========================================
# (build_or_load_index, chinese_tokenizer, _get_target_node_ids は変更なし)

def build_or_load_index() -> Optional[VectorStoreIndex]:
    # (省略: 前回のコードと同じ)
    if not os.path.exists(PERSIST_DIR):
        logger.info("🆕 新規インデックス構築開始")
        all_documents = []
        azure_endpoint, azure_key = load_azure_config()
        if azure_endpoint and azure_key and os.path.exists(DOCS_DIR):
            azure_reader = LocalAzureReader(azure_endpoint, azure_key)
            excel_reader = LocalExcelReader()
            text_reader = LocalTextReader()
            file_extractor = {
                ".pdf": azure_reader, 
                ".xlsx": excel_reader, 
                ".docx": azure_reader, 
                ".pptx": azure_reader,
                ".jpg": azure_reader, 
                ".png": azure_reader,
                ".txt": text_reader,  
                ".md": text_reader    
            }
            try:
                raw_docs = SimpleDirectoryReader(DOCS_DIR, file_extractor=file_extractor, recursive=True).load_data()
                all_documents.extend(raw_docs)
            except Exception: pass
        json_reader = LocalJSONChunkReader()
        json_docs = json_reader.load_data(LONG_DOC_OUTPUT_DIR)
        all_documents.extend(json_docs)
        if not all_documents: return None
        try:
            index = VectorStoreIndex.from_documents(all_documents, show_progress=True)
            index.storage_context.persist(persist_dir=PERSIST_DIR)
            return index
        except Exception: return None
    else:
        try:
            storage_context = StorageContext.from_defaults(persist_dir=PERSIST_DIR)
            return load_index_from_storage(storage_context)
        except Exception: return None

def chinese_tokenizer(text: str) -> List[str]:
    # Jiebaに専門用語を追加して切れ方を改善する
    # 辞書登録（簡易的）
    for w in ["サプライチェーン", "水素燃料電池", "FCEV", "バリューチェーン"]:
        jieba.add_word(w)
        
    text = text.lower()
    tokens = jieba.lcut_for_search(text)
    return [t for t in tokens if len(t.strip()) > 0]

def _get_target_node_ids(index: VectorStoreIndex, target_filenames: List[str], target_doc_type: str) -> List[str]:
    matched_ids = []
    for node in index.docstore.docs.values():
        if node.metadata.get("doc_type") != target_doc_type: continue
        f_name = node.metadata.get("file_name", "")
        is_hit = False
        if f_name in target_filenames: is_hit = True
        else:
            for t in target_filenames:
                if f_name.endswith(t) or t.endswith(f_name):
                    is_hit = True
                    break
        if is_hit: matched_ids.append(node.node_id)
    return matched_ids

def query_knowledge_base(
    index: Optional[VectorStoreIndex], 
    query_text: str, 
    target_filenames: Optional[List[str]] = None,
    top_k_retrieval: int = 30, 
    top_k_final: int = 7,      
    bm25_weight: float = 0.5,
    debug_mode: bool = True
) -> Tuple[str, List[str], List[str]]:
    if index is None: return "（ナレッジベース未初期化）", [], []

    try:
        if debug_mode:
            logger.info(f"🔎 検索クエリ: {query_text}")
            tokens = chinese_tokenizer(query_text)
            logger.info(f"🔎 Query Tokens: {tokens}")

        # 0. Vector Search設定
        if target_filenames:
            short_node_ids = _get_target_node_ids(index, target_filenames, "short_doc")
            long_node_ids = _get_target_node_ids(index, target_filenames, "long_doc")
            
            vector_retriever_short = VectorIndexRetriever(index=index, similarity_top_k=top_k_retrieval, node_ids=short_node_ids)
            vector_retriever_long = VectorIndexRetriever(index=index, similarity_top_k=top_k_retrieval, node_ids=long_node_ids)
            if debug_mode: logger.info(f"🎯 Target Node IDs - Short: {len(short_node_ids)}, Long: {len(long_node_ids)}")
        else:
            vector_retriever_short = VectorIndexRetriever(index=index, similarity_top_k=top_k_retrieval, filters=MetadataFilters(filters=[ExactMatchFilter(key="doc_type", value="short_doc")]))
            vector_retriever_long = VectorIndexRetriever(index=index, similarity_top_k=top_k_retrieval, filters=MetadataFilters(filters=[ExactMatchFilter(key="doc_type", value="long_doc")]))

        # 1. Custom BM25 Retriever
        bm25_nodes = []
        all_nodes_dict = index.docstore.docs
        if target_filenames:
            for node in all_nodes_dict.values():
                node_fname = node.metadata.get("file_name", "")
                if node_fname in target_filenames:
                    bm25_nodes.append(node)
                    continue
                for target in target_filenames:
                    if node_fname.endswith(target) or target.endswith(node_fname):
                        bm25_nodes.append(node)
                        break
        else:
            bm25_nodes = list(all_nodes_dict.values())

        if bm25_nodes:
            # ★ 変更点: 自作のLocalBM25Retrieverを使用
            bm25_retriever = LocalBM25Retriever(
                nodes=bm25_nodes,
                tokenizer=chinese_tokenizer,
                similarity_top_k=top_k_retrieval * 2
            )
        else:
            bm25_retriever = None

        # 2. 検索実行
        if debug_mode:
            try:
                nodes_short_debug = vector_retriever_short.retrieve(query_text)
                _log_debug_nodes("1. Vector Search (Short Docs)", nodes_short_debug)
                
                nodes_long_debug = vector_retriever_long.retrieve(query_text)
                _log_debug_nodes("2. Vector Search (Long Docs)", nodes_long_debug)
                
                if bm25_retriever:
                    nodes_bm25_debug = bm25_retriever.retrieve(query_text)
                    _log_debug_nodes("3. BM25 Search (Custom)", nodes_bm25_debug)
            except Exception as e: logger.warning(f"デバッグログ出力中エラー: {e}")

        retrievers = [vector_retriever_short, vector_retriever_long]
        if bm25_retriever:
            retrievers.append(bm25_retriever)

        fusion_retriever = QueryFusionRetriever(
            retrievers,
            similarity_top_k=top_k_retrieval * 2,
            num_queries=1, 
            use_async=False
        )

        nodes = fusion_retriever.retrieve(query_text)

        # 3. フィルタリング
        if target_filenames and nodes:
            filtered_nodes = []
            for node in nodes:
                node_fname = node.metadata.get("file_name")
                is_hit = False
                if node_fname in target_filenames: is_hit = True
                else:
                    for target in target_filenames:
                        if node_fname.endswith(target) or target.endswith(node_fname):
                            is_hit = True; break
                if is_hit: filtered_nodes.append(node)
            nodes = filtered_nodes
            if debug_mode: logger.info(f"📉 最終フィルタ後の候補数: {len(nodes)} 件")

        if not nodes: return "（指定ファイルに関連情報なし）", [], []

        # 4. Rerank
        reranker = LocalSentenceTransformerRerank(model_name=RERANK_MODEL_NAME, top_n=top_k_final)
        reranked_nodes = reranker.postprocess_nodes(nodes, query_bundle=QueryBundle(query_text))

        if debug_mode: _log_debug_nodes("5. After Rerank (Final Result)", reranked_nodes, top_n=top_k_final)
        if not reranked_nodes: return "（関連ドキュメントなし）", [], []

        # 5. 整形
        retrieved_contexts = [node.node.get_content() for node in reranked_nodes]
        result_text = ""
        hit_files = []
        for i, node_with_score in enumerate(reranked_nodes):
            node = node_with_score.node
            score = node_with_score.score
            meta = node.metadata
            file_name = meta.get('file_name', 'unknown')
            page_nums = meta.get('page_numbers', [])
            section = meta.get('section_title', '')
            citation_tag = f"[{file_name} p.{', '.join(map(str, page_nums))}]" if page_nums else f"[{file_name}]"
            header_info = f" (Section: {section})" if section else ""
            result_text += f"\n--- Reference {i+1} {citation_tag}{header_info} (Confidence: {score:.4f}) ---\n{node.get_content().strip()}\n"
            if file_name not in hit_files: hit_files.append(file_name)

        return result_text, hit_files, retrieved_contexts

    except Exception as e:
        logger.error(f"検索プロセスエラー: {e}", exc_info=True)
        return f"検索エラー: {e}", [], []

def get_all_indexed_filenames(index: Optional[VectorStoreIndex]) -> Set[str]:
    if index is None: return set()
    try:
        return {doc.metadata.get('file_name') for doc in index.docstore.docs.values() if doc.metadata.get('file_name')}
    except: return set()

if __name__ == "__main__":
    idx = build_or_load_index()
    if idx:
        print(">>> テストクエリ実行")
        res, files, ctxs = query_knowledge_base(idx, "水素燃料電池", target_filenames=None, debug_mode=True)
        print("\n=== 最終結果 ===")
        print(res)