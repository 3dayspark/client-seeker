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
from llama_index.core.schema import NodeWithScore
# メタデータフィルタリング用
from llama_index.core.vector_stores import MetadataFilters, ExactMatchFilter, FilterCondition
from llama_index.core.retrievers import VectorIndexRetriever
from llama_index.core.postprocessor.types import BaseNodePostprocessor

# --- Hybrid Search & Embedding ---
from llama_index.retrievers.bm25 import BM25Retriever
from llama_index.core.retrievers import QueryFusionRetriever

# --- Reranking用 ---
from sentence_transformers import CrossEncoder
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ロガー設定（INFOレベルで詳細を表示するように設定）
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Azure/Urllibのログは抑制
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# --- 定数設定 ---
DOCS_DIR = "knowledge_docs"
LONG_DOC_OUTPUT_DIR = os.path.join("long_doc_process", "data_output") 
PERSIST_DIR = "storage_index"
EMBED_MODEL_NAME = "BAAI/bge-m3"
RERANK_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2" 
API_KEYS_FILE = "api_keys.json"

# --- グローバル設定 ---
Settings.llm = None
Settings.embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)


# ==========================================
# 0. デバッグ用ヘルパー関数
# ==========================================

def _log_debug_nodes(stage_name: str, nodes: List[NodeWithScore], top_n: int = 3):
    """
    各検索ステージの状態を詳細にログ出力するためのヘルパー関数
    """
    logger.info(f"\n🔍 --- [DEBUG: {stage_name}] Top {top_n} ---")
    if not nodes:
        logger.info("   (結果なし)")
        return

    for i, node in enumerate(nodes[:top_n]):
        meta = node.node.metadata
        file_name = meta.get('file_name', 'unknown')
        doc_type = meta.get('doc_type', 'unknown')
        score = node.score if node.score is not None else 0.0
        
        # テキストのプレビュー（改行を除去して短く表示）
        content_preview = node.node.get_content().replace('\n', ' ')[:50] + "..."
        
        logger.info(
            f"   #{i+1} [Score: {score:.4f}] "
            f"File: {file_name} ({doc_type}) | Text: {content_preview}"
        )
    logger.info("------------------------------------------------")


# ==========================================
# 1. カスタムコンポーネント定義
# ==========================================

class LocalJSONChunkReader:
    """
    pdf_processor.py で生成されたJSONチャンクファイルを読み込むリーダー
    （長文ドキュメント用）
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
                    

                    metadata["doc_type"] = "long_doc"

                    doc = Document(
                        text=text,
                        metadata=metadata,
                        excluded_llm_metadata_keys=["file_path", "doc_type"], 
                        excluded_embed_metadata_keys=["page_numbers", "doc_type"] 
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
        
        scores = self.model.predict(pairs)
        
        for node, score in zip(nodes, scores):
            node.score = float(score)

        sorted_nodes = sorted(nodes, key=lambda x: x.score, reverse=True)
        return sorted_nodes[:self.top_n]

class LocalExcelReader:
    """
    Excelファイルを読み込むリーダー（短文・構造化データ用）
    """
    def load_data(self, file_path: str, extra_info: Optional[dict] = None) -> List[Document]:
        try:
            dfs = pd.read_excel(file_path, sheet_name=None)
            full_text = ""
            for sheet_name, df in dfs.items():
                full_text += f"\n## Sheet: {sheet_name}\n"
                markdown_table = df.fillna("").to_markdown(index=False)
                full_text += markdown_table + "\n"
            

            return [Document(
                text=full_text, 
                metadata={
                    "file_name": os.path.basename(file_path),
                    "doc_type": "short_doc"
                }
            )]
        except Exception as e:
            logger.error(f"Excel解析エラー {file_path}: {e}")
            return []

class LocalAzureReader:
    """
    Azure Document Intelligenceを使用してPDF/画像を読み込むリーダー（短文用）
    """
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
                

                return [Document(
                    text=result.content, 
                    metadata={
                        "file_name": full_name,
                        "doc_type": "short_doc"
                    }
                )]
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
    if not os.path.exists(PERSIST_DIR):
        logger.info("🆕 新規インデックス構築を開始します...")
        
        all_documents = []

        # 1. 既存 Knowledge Docs の読み込み (Short Docs)
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
                logger.info(f"✅ Knowledge Docs (Short): {len(raw_docs)} 件ロード完了")
            except Exception as e:
                logger.error(f"Knowledge Docs ロード失敗: {e}")

        # 2. Long Doc (pdf_processor output) の読み込み (Long Docs)
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
    target_filenames: Optional[List[str]] = None,
    top_k_retrieval: int = 10, 
    top_k_final: int = 3,      
    bm25_weight: float = 0.5,
    debug_mode: bool = True  # デバッグモード（詳細ログ出力）
) -> Tuple[str, List[str], List[str]]:
    """
    Hybrid Search with Federated Retrieval strategy (Split Short/Long docs)
    長文ドキュメントによる検索結果の占有を防ぐため、強制的に短文・長文を個別に検索して統合します。
    """
    if index is None:
        return "（ナレッジベース未初期化）", [], []

    try:
        if debug_mode:
            logger.info(f"🔎 検索クエリ: {query_text}")


        # ==========================================
        # 0. フィルタリング設定の構築
        # ==========================================
        base_filters_short = [ExactMatchFilter(key="doc_type", value="short_doc")]
        base_filters_long = [ExactMatchFilter(key="doc_type", value="long_doc")]
        
        # ファイル名指定がある場合の追加フィルタ
        # SQLでいう WHERE doc_type='...' AND (file_name='A' OR file_name='B' ...)
        extra_filters = []
        condition = FilterCondition.AND # デフォルトはAND（既存条件と合わせるため）

        if target_filenames:
            # 複数ファイルに対応するため OR 条件のフィルタグループを作成
            file_filters = [ExactMatchFilter(key="file_name", value=fname) for fname in target_filenames]
            # 注意: LlamaIndexのバージョンによってはネストされたフィルタの書き方が異なる場合がありますが、
            # 標準的なMetadataFiltersの使用法として、file_nameフィルタを適用します。
            # ここではシンプルに「Retrieval後にフィルタリング」するか、「VectorStoreのフィルタ機能」を使うかですが、
            # 効率のためVectorStoreフィルタを使います。
            pass 

        # LlamaIndexのMetadataFiltersは複雑な AND/OR のネストが難しい場合があるため、
        # ここでは target_filenames がある場合、リスト内のいずれかのファイルにマッチさせるロジックを組みます。
        
        def _build_filters(base_list, file_targets):
            if not file_targets:
                return MetadataFilters(filters=base_list)
            
            # base_list (AND) + (file_name IN file_targets (OR))
            # VectorIndexRetriever は単層のフィルタリストを受け取るのが基本なので、
            # 「doc_typeが一致」かつ「file_nameがリストに含まれる」ノードを取得する必要があります。
            
            # 最も確実な方法は、filtersパラメータに file_name の OR 条件を渡すことですが、
            # doc_type との AND が必要です。
            # 簡易実装として: Retrieval後にPython側でフィルタリングする方が安全かつ確実です。
            # しかし、パフォーマンスを考慮し、ここではMetadataFiltersの condition=OR を使いつつ、
            # doc_type も含めた全組み合わせを展開する方法をとります。
            
            # 例: (doc_type=short AND file=A) OR (doc_type=short AND file=B) ...
            combined_filters = []
            doc_type_val = base_list[0].value # "short_doc" or "long_doc"
            
            for fname in file_targets:
                combined_filters.append(
                    ExactMatchFilter(key="file_name", value=fname)
                )
            
            # 注意: ここで doc_type フィルタと file_name フィルタをどう組み合わせるかは
            # VectorStoreの実装によります。ここではシンプルに
            # 「Retrieval時は file_name でフィルタし、doc_type は後で確認」または
            # target_filenames がある場合は doc_type フィルタを無視して file_name だけで絞る（ファイル名が一意ならこれでOK）
            # という戦略をとります。通常ファイル名は一意と仮定します。
            
            return MetadataFilters(
                filters=combined_filters,
                condition=FilterCondition.OR
            )

        # ==========================================
        # 1. 各Retrieverの準備
        # ==========================================
        
        # A. 短文ドキュメント用 Retriever
        # target_filenamesがある場合は、それらのファイルだけをOR条件で検索
        filters_short = _build_filters(base_filters_short, target_filenames) if target_filenames else MetadataFilters(filters=base_filters_short)
        
        vector_retriever_short = VectorIndexRetriever(
            index=index,
            similarity_top_k=top_k_retrieval,
            filters=filters_short
        )

        # B. 長文ドキュメント用 Retriever
        filters_long = _build_filters(base_filters_long, target_filenames) if target_filenames else MetadataFilters(filters=base_filters_long)

        vector_retriever_long = VectorIndexRetriever(
            index=index,
            similarity_top_k=top_k_retrieval,
            filters=filters_long
        )
        
        # C. BM25 Retriever
        # BM25は通常MetadataFilterをネイティブサポートしていないため、全検索後にPython側でフィルタします
        bm25_retriever = BM25Retriever.from_defaults(
            docstore=index.docstore,
            similarity_top_k=top_k_retrieval * 2 # フィルタされる分多めに取得
        )

        # 詳細ログ出力セクション
        if debug_mode:
            try:
                # Short Vector 確認
                nodes_short_debug = vector_retriever_short.retrieve(query_text)
                _log_debug_nodes("1. Vector Search (Short Docs)", nodes_short_debug)
                
                # Long Vector 確認
                nodes_long_debug = vector_retriever_long.retrieve(query_text)
                _log_debug_nodes("2. Vector Search (Long Docs)", nodes_long_debug)
                
                # BM25 確認
                nodes_bm25_debug = bm25_retriever.retrieve(query_text)
                _log_debug_nodes("3. BM25 Search (Global)", nodes_bm25_debug)
            except Exception as e:
                logger.warning(f"デバッグログ出力中にエラーが発生しましたが、処理は継続します: {e}")

        # 2. Hybrid Search (Fusion) の実行
        fusion_retriever = QueryFusionRetriever(
            [vector_retriever_short, vector_retriever_long, bm25_retriever],
            similarity_top_k=top_k_retrieval * 2, # Reranker用に多めに候補を残す
            num_queries=1, 
            use_async=False
        )

        nodes = fusion_retriever.retrieve(query_text)

        # ==========================================
        # 2. Python側での厳密なフィルタリング (特にBM25対策)
        # ==========================================
        if target_filenames:
            filtered_nodes = []
            for node in nodes:
                node_fname = node.metadata.get("file_name")
                if node_fname in target_filenames:
                    filtered_nodes.append(node)
                # パスが含まれる場合の揺らぎ吸収 (例: dir/file.pdf vs file.pdf)
                else:
                    for target in target_filenames:
                        if node_fname.endswith(target) or target.endswith(node_fname):
                            filtered_nodes.append(node)
                            break
            nodes = filtered_nodes
            
            if debug_mode:
                logger.info(f"📉 フィルタ後の候補数: {len(nodes)} 件")

        if not nodes:
             return "（指定されたファイルに関連情報は含まれていませんでした）", [], []

        # 3. Reranking (Cross-Encoder) の実行
        reranker = LocalSentenceTransformerRerank(
            model_name=RERANK_MODEL_NAME,
            top_n=top_k_final
        )
        
        reranked_nodes = reranker.postprocess_nodes(
            nodes, 
            query_bundle=QueryBundle(query_text)
        )

        if debug_mode:
            _log_debug_nodes("5. After Rerank (Final)", reranked_nodes, top_n=top_k_final)

        if not reranked_nodes:
            return "（関連ドキュメントなし）", [], []

        # for RAGAS or Context
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
        return f"検索エラー: {e}", [], []

def get_all_indexed_filenames(index: Optional[VectorStoreIndex]) -> Set[str]:
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
    # 注意: 初回実行時またはReaderロジック変更時は storage_index を削除してください
    if os.path.exists(PERSIST_DIR):
        logger.info(f"ℹ️ {PERSIST_DIR} が存在します。doc_typeメタデータを反映するにはフォルダを削除して再構築してください。")

    idx = build_or_load_index()
    if idx:
        print(">>> クエリ実行開始")
        res, files, ctxs = query_knowledge_base(idx, "水素燃料電池のサプライチェーン", debug_mode=True)
        print("\n=== 最終結果 ===")
        print(res)
        
        # 外部参照用関数のテスト
        all_files = get_all_indexed_filenames(idx)
        print(f"\nインデックス済みファイル数: {len(all_files)}")