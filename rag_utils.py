
import os
import json
import logging
from typing import Optional, Tuple, List
import pandas as pd

# --- Azure SDK のインポート ---
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

# --- LlamaIndex コアコンポーネント ---
from llama_index.core import (
    VectorStoreIndex,
    SimpleDirectoryReader,
    StorageContext,
    load_index_from_storage,
    Document,
    Settings
)
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ロガーの設定
logger = logging.getLogger(__name__)

# Azure SDK の内部ログを抑制する設定
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
# urllib3 も抑制
logging.getLogger("urllib3").setLevel(logging.WARNING)

# --- 設定定数 ---
DOCS_DIR = "knowledge_docs"
PERSIST_DIR = "storage_index"  # インデックスの永続化ディレクトリ
EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"  # 採用するEmbeddingモデル
API_KEYS_FILE = "api_keys.json"

# --- グローバル設定 ---
# ローカルEmbeddingを強制使用し、LLM（OpenAI等）は無効化する（検索機能のみ使用するため）
Settings.llm = None
Settings.embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)



class LocalExcelReader:
    def load_data(self, file_path: str, extra_info: Optional[dict] = None) -> List[Document]:
        """
        pandas を使用して Excel を読み込み、Markdown 形式のテキストに変換します。
        """
        try:
            # 全シートを読み込む
            dfs = pd.read_excel(file_path, sheet_name=None)
            full_text = ""
            
            for sheet_name, df in dfs.items():
                full_text += f"\n## Sheet: {sheet_name}\n"
                # DataFrame を Markdown の表に変換 (tabulateが必要)
                # 空のセルは空文字にするなどのクリーニングを行う
                markdown_table = df.fillna("").to_markdown(index=False)
                full_text += markdown_table + "\n"
            
            # ドキュメントオブジェクトを返す
            return [Document(text=full_text, metadata={"file_name": os.path.basename(file_path)})]
        except Exception as e:
            logger.error(f"Excel解析エラー {file_path}: {e}")
            return []

# --- 1. 設定読み込み関数 ---
def load_azure_config():
    """
    api_keys.json から Azure の設定を読み込みます。
    業界標準に従い、設定とロジックを分離し、ファイル不在時の例外処理を含みます。
    
    Returns:
        tuple: (endpoint, key) 取得失敗時は (None, None)
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(current_dir, API_KEYS_FILE)

    if not os.path.exists(json_path):
        logger.error(f"重大なエラー：設定ファイル {API_KEYS_FILE} が見つかりません。")
        return None, None

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        azure_conf = data.get("azure", {})
        key = azure_conf.get("key", "")
        endpoint = azure_conf.get("endpoint", "")

        if not key or not endpoint:
            logger.error(f"設定エラー：{API_KEYS_FILE} 内に azure.key または azure.endpoint が見つかりません。")
            return None, None
            
        return endpoint, key

    except Exception as e:
        logger.error(f"設定ファイルの読み込みに失敗しました: {e}")
        return None, None

# --- 2. Azure Reader クラスの手動定義 ---
# (llama-index-readers-azure パッケージに依存せず、公式SDKをラップして使用)
class LocalAzureReader:
    def __init__(self, api_endpoint: str, api_key: str):
        self.client = DocumentIntelligenceClient(
            endpoint=api_endpoint, 
            credential=AzureKeyCredential(api_key)
        )

    def load_data(self, file_path: str, extra_info: Optional[dict] = None) -> List[Document]:
            try:
                # ファイル名だけでなく、親フォルダ名も含める工夫
                # 例: knowledge_docs/2023決算/p1.png -> metadataには "2023決算/p1.png" と記録
                full_name = os.path.relpath(file_path, DOCS_DIR)
                
                with open(file_path, "rb") as f:
                    poller = self.client.begin_analyze_document(
                        model_id="prebuilt-layout", 
                        body=f,  
                        output_content_format="markdown"
                    )
                result = poller.result()
                text_content = result.content
                
                # metadataに full_name をセット
                return [Document(text=text_content, metadata={"file_name": full_name})]
            except Exception as e:
                logger.error(f"Azureによる {file_path} の解析中にエラーが発生しました: {e}")
                return []


def build_or_load_index() -> Optional[VectorStoreIndex]:
    """
    インデックスを初期化します。
    ローカルに保存されたインデックスが存在すればロードし、
    存在しなければ Azure AI Document Intelligence を使用して新規に構築します。
    """
    if not os.path.exists(PERSIST_DIR):
        logger.info("ローカルインデックスが見つかりません。Azure AI Document Intelligence を使用して解析を開始します...")
        
        # --- 設定の動的読み込み ---
        azure_endpoint, azure_key = load_azure_config()
        
        if not azure_endpoint or not azure_key:
            logger.warning("Azureの設定が不足しているため、インデックスを構築できません。")
            return None

        if not os.path.exists(DOCS_DIR):
            os.makedirs(DOCS_DIR)
            logger.warning(f"ドキュメントディレクトリが存在しません。{DOCS_DIR} にドキュメントを配置してください。")
            return None

        # カスタムReaderの初期化
        azure_reader = LocalAzureReader(azure_endpoint, azure_key)
        excel_reader = LocalExcelReader() 

        # 複雑なフォーマットをすべて Azure Reader にマッピング
        file_extractor = {
            ".pdf": azure_reader,
            ".xlsx": excel_reader,  # ★ Excelは pandas で読む
            ".docx": azure_reader,
            ".pptx": azure_reader,
            ".jpg": azure_reader,
            ".jpeg": azure_reader,
            ".png": azure_reader,
            ".bmp": azure_reader,
            ".tiff": azure_reader
        }

        try:
            # SimpleDirectoryReader は file_extractor で指定された拡張子に対して
            # load_data メソッドを自動的に呼び出します
            documents = SimpleDirectoryReader(
                DOCS_DIR, 
                file_extractor=file_extractor,
                recursive=True
            ).load_data()

            if not documents:
                logger.warning("ディレクトリが空か、解析に失敗しました。")
                return None
            
            # 解析結果のプレビュー（構造保持の確認用）
            if documents:
                logger.info(f"🔍 Azure 解析結果プレビュー (Markdown): \n{documents[0].text[:500]}...")

            index = VectorStoreIndex.from_documents(documents)
            index.storage_context.persist(persist_dir=PERSIST_DIR)
            logger.info("インデックスの構築と保存が完了しました。")
            return index

        except Exception as e:
            logger.error(f"インデックス構築中にエラーが発生しました: {e}")
            return None
    else:
        logger.info("ローカルインデックスをロードしています...")
        try:
            storage_context = StorageContext.from_defaults(persist_dir=PERSIST_DIR)
            index = load_index_from_storage(storage_context)
            logger.info("インデックスのロードに成功しました。")
            return index
        except Exception as e:
            logger.error(f"インデックスのロードに失敗しました: {e}")
            return None

def query_knowledge_base(
    index: Optional[VectorStoreIndex], 
    query_text: str, 
    k: int = 3, 
    score_threshold: float = 0.4
) -> Tuple[str, List[str]]:
    """
    ナレッジベースからクエリに関連する情報を検索します。
    類似度スコアが閾値（score_threshold）未満の結果は除外されます。

    Args:
        index (VectorStoreIndex): 検索対象のインデックス
        query_text (str): 検索クエリ
        k (int): 最大取得件数
        score_threshold (float): 類似度の下限閾値

    Returns:
        Tuple[str, List[str]]: (整形された検索結果テキスト, ヒットしたファイル名のリスト)
    """
    if index is None:
        logger.warning("インデックスがNoneのため、検索を実行できません。")
        return "（ナレッジベースが初期化されていないか、空です）", []

    try:
        # Top-k検索の実行
        retriever = index.as_retriever(similarity_top_k=k)
        nodes = retriever.retrieve(query_text)
        
        valid_nodes = []
        hit_filenames = [] # ファイル名を保存するリスト

        for node in nodes:
            # デバッグ用ログ
            logger.info("File: %s | Score: %.4f", node.metadata.get('file_name'), node.score)
            
            # 閾値によるフィルタリング
            if node.score >= score_threshold:
                valid_nodes.append(node)
                
                # ファイル名を取得してリストに追加（重複排除）
                fname = node.metadata.get('file_name', 'unknown')
                if fname not in hit_filenames:
                    hit_filenames.append(fname)
        
        if not valid_nodes:
            logger.info("閾値(%.2f)を超える関連ドキュメントが見つかりませんでした。", score_threshold)
            return "（関連するドキュメントが見つかりませんでした。）", []

        result_text = ""
        for i, node in enumerate(valid_nodes):
            file_name = node.metadata.get('file_name', 'unknown')
            result_text += f"\n--- 参考ドキュメント {i+1} (ソース: {file_name}) ---\n{node.text}\n"
            
        return result_text, hit_filenames

    except Exception as e:
        logger.error(f"検索処理中に例外が発生しました: {e}")
        return f"検索エラー: {e}", []