

import asyncio
import itertools
import json
import logging
import os
import re
import sys
import traceback
import uuid
from typing import List, Dict, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import requests  # Gemini API用
from openai import OpenAI  # ModelScope API用

# 独自のRAGユーティリティをインポート
from rag_utils import build_or_load_index, query_knowledge_base, get_all_indexed_filenames
# データベース接続をインポート
from database import get_database_schema_info, execute_raw_sql

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# playwright_test モジュールの動的インポート
# 実行環境のカレントディレクトリをパスに追加してインポートを試みる
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.append(current_dir)
    import playwright_test
finally:
    if current_dir in sys.path:
        sys.path.remove(current_dir)

app = FastAPI()

# --- CORS設定 ---
origins = [
    "http://localhost",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://192.168.1.41:5173",
    "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 定数・設定 (playwright_test.pyの設定を利用) ---
MODEL_SCOPE_API_KEY = playwright_test.MODEL_SCOPE_API_KEY
MODEL_SCOPE_BASE_URL = playwright_test.MODEL_SCOPE_BASE_URL
MODEL_SCOPE_MODEL_ID = playwright_test.MODEL_SCOPE_MODEL_ID

GEMINI_API_KEYS = playwright_test.GEMINI_API_KEYS
GEMINI_API_URL = playwright_test.GEMINI_API_URL
USE_GEMINI_AS_LLM = False

# --- グローバル変数 ---
modelscope_client = None
gemini_api_key_pool = None
gemini_base_headers = {"Content-Type": "application/json"}
rag_index = None
VALID_FILENAMES = set() 

# チャット履歴管理
# 本番環境ではRedisやDBへの移行を推奨
# 構造: { session_id: [ {"role": "user"|"assistant"|"tool", "content": "..."} ] }
CHAT_SESSIONS: Dict[str, List[Dict[str, str]]] = {}

# --- 初期化処理 ---
def _init_llm_clients():
    """LLMクライアントの初期化を行います。"""
    global modelscope_client, gemini_api_key_pool

    # ModelScopeの初期化
    if not modelscope_client and not USE_GEMINI_AS_LLM:
        try:
            modelscope_client = OpenAI(
                base_url=MODEL_SCOPE_BASE_URL,
                api_key=MODEL_SCOPE_API_KEY,
            )
            logger.info("チャット用 ModelScope クライアントが初期化されました。")
        except Exception as e:
            logger.error(f"ModelScope 初期化失敗: {e}")

    # Geminiの初期化
    if not gemini_api_key_pool and USE_GEMINI_AS_LLM:
        try:
            gemini_api_key_pool = itertools.cycle(GEMINI_API_KEYS)
            logger.info("チャット用 Gemini クライアントが初期化されました。")
        except Exception as e:
            logger.error(f"Gemini 初期化失敗: {e}")

_init_llm_clients()

# RAGインデックスの構築・ロード
rag_index = build_or_load_index()


# ---------------------------------------------------------
# ヘルパー: JSON抽出と修復
# ---------------------------------------------------------
def extract_json_from_text(text: str) -> Dict[str, Any]:
    """
    LLMの回答からJSONブロックを抽出し、構文エラー（特に改行コード）を強力に自動修復してパースします。
    """
    try:
        # 1. Markdownのコードブロック記法を除去
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```', '', text)
        text = text.strip()

        # 2. 最初に見つかった { ... } のペアを探す (最長一致)
        # 単純なregexではなく、ネストに対応した簡易抽出、または単純に { で始まり } で終わる範囲を探す
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        
        if start_idx != -1 and end_idx != -1:
            json_str = text[start_idx : end_idx + 1]
        else:
            json_str = text

        # 3. JSON文字列内の「不正な改行」を「\\n」に置換する処理
        # JSONの仕様では、ダブルクォートで囲まれた文字列の中で生の改行は許されないため、
        # これを検知してエスケープします。
        
        new_chars = []
        in_string = False
        escape = False
        
        for char in json_str:
            if char == '"' and not escape:
                in_string = not in_string
                new_chars.append(char)
            elif char == '\\' and not escape:
                escape = True
                new_chars.append(char)
            elif in_string and char == '\n':
                # 文字列内部の改行はエスケープする
                new_chars.append('\\n')
                escape = False
            elif in_string and char == '\r':
                # \r は無視するかスペースにする
                pass 
                escape = False
            elif in_string and char == '\t':
                new_chars.append('\\t')
                escape = False
            else:
                new_chars.append(char)
                if escape:
                    escape = False
        
        json_str_clean = "".join(new_chars)

        return json.loads(json_str_clean)

    except json.JSONDecodeError as e:
        logger.warning(f"JSON Parse Error: {e} | Raw: {text[:100]}...")
        # 最後の手段：改行をすべて消してトライしてみる（整形崩れるが動作優先）
        try:
             # 簡易的な修復：制御文字を削除
             simple_clean = re.sub(r'[\x00-\x1f]', ' ', text)
             match = re.search(r'(\{[\s\S]*\})', simple_clean)
             if match:
                 return json.loads(match.group(1))
        except:
            pass
        return None
    except Exception as e:
        logger.error(f"Extract Error: {e}")
        return None


def sanitize_citations(text: str) -> str:
    """
    テキスト内の引用タグ [filename p.x] を検査し、
    実在しないファイル名のタグを削除します。
    """
    if not text:
        return ""
        
    # 正規表現: [任意のファイル名 p.数字] または [任意のファイル名]
    # キャプチャグループ1: ファイル名部分
    # キャプチャグループ2: (オプション) ページ番号部分
    # 例: [test.pdf p.1] -> group1="test.pdf"
    pattern = r'\[(.*?)(?: p\.[\d,\s]+)?\]'
    
    def validator(match):
        full_tag = match.group(0) # [test.pdf p.1]
        filename = match.group(1).strip() # test.pdf
        
        # 1. 完全一致チェック
        if filename in VALID_FILENAMES:
            return full_tag
            
        # 2. パスが含まれる場合の揺らぎ吸収 (例: knowledge_docs/test.pdf -> test.pdf)
        # RAGのインデックスには相対パスで入っている可能性があるため、末尾一致も確認
        for valid_name in VALID_FILENAMES:
            if filename.endswith(valid_name) or valid_name.endswith(filename):
                return full_tag
        
        # 検証に失敗した場合、タグを削除（空文字を返す）
        # ※ ログに残すとデバッグしやすい
        logger.warning(f"🚫 Removing hallucinated citation: {full_tag}")
        return ""

    # 置換実行
    return re.sub(pattern, validator, text)

# --- 補助関数: LLM 呼び出し (Master Brain) ---
async def _call_master_llm(prompt: str, history: List[Dict[str, str]]) -> str:
    """
    LLM を呼び出して応答を生成します。履歴をプロンプトに統合します。
    """
    # 1. コンテキスト文字列の構築
    history_text = ""
    # トークン制限を考慮し、最新の10件のみを取得
    recent_history = history[-10:] if len(history) > 10 else history

    for msg in recent_history:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "user":
            history_text += f"User: {content}\n"
        elif role == "assistant":
            history_text += f"Assistant: {content}\n"
        elif role == "tool":
            readable_content = content.replace("||NEWLINE||", "\n").replace("||REASON||", " [判断根拠: ")
            if " [判断根拠: " in readable_content:
                readable_content = readable_content.replace("\n", "]\n")
            history_text += f"System (Tool Execution Result): \n{readable_content}\n"

    full_prompt = f"""
    {prompt}
    
    --- チャット履歴 (Chat History) ---
    {history_text}
    
    Assistant:
    """

    # ModelScope の呼び出し
    if not USE_GEMINI_AS_LLM and modelscope_client:
        try:
            response = await asyncio.to_thread(
                modelscope_client.chat.completions.create,
                model=MODEL_SCOPE_MODEL_ID,
                messages=[{'role': 'user', 'content': full_prompt}],
                stream=False,
                extra_body={"enable_thinking": False}
            )
            if hasattr(response.choices[0].message, 'content'):
                return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"ModelScope Call Error: {e}")
            return f"Error calling ModelScope: {e}"

    # Gemini の呼び出し
    elif USE_GEMINI_AS_LLM and gemini_api_key_pool:
        try:
            current_key = next(gemini_api_key_pool)
            headers = gemini_base_headers.copy()
            headers["X-goog-api-key"] = current_key
            payload = {"contents": [{"parts": [{"text": full_prompt}]}]}

            response = await asyncio.to_thread(
                requests.post, GEMINI_API_URL, headers=headers, json=payload, timeout=60
            )
            response.raise_for_status()
            data = response.json()
            return data['candidates'][0]['content']['parts'][0]['text'].strip()
        except Exception as e:
            return f"Error calling Gemini: {e}"

    return "Error: No LLM client available."




class PlaywrightLogger:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.original_stdout = sys.__stdout__

    def write(self, message):
        self.original_stdout.write(message)
        for line in message.splitlines():
            line = line.strip()
            if line:
                try:
                    self.queue.put_nowait(f"data: {line}\n\n")
                except asyncio.QueueFull:
                    pass

    def flush(self):
        self.original_stdout.flush()
        try:
            self.queue.put_nowait("data: \n\n")
        except:
            pass

    def log_to_frontend(self, message: str):
        # 最適化：スクリーンショットデータの場合、バックエンドのコンソールには出力しない（遅延防止）
        if "[SCREENSHOT]" not in message:
            self.original_stdout.write(message + '\n')
            self.original_stdout.flush()

        try:
            self.queue.put_nowait(f"data: {message.strip()}\n\n")
        except:
            pass



# --- 起動時のスキーマロード ---
db_schema_context = ""

@app.on_event("startup")
async def startup_event():
    global db_schema_context, VALID_FILENAMES
    # 起動時にスキーマ情報を取得・キャッシュ
    db_schema_context = await get_database_schema_info()
    logger.info("System Context: DB Schema loaded.")

    #RAGインデックスからファイル名リストをキャッシュ
    if rag_index:
        VALID_FILENAMES = get_all_indexed_filenames(rag_index)
        logger.info(f"System Context: Loaded {len(VALID_FILENAMES)} valid filenames from RAG index.")


# ---------------------------------------------------------
# コア: Master Agent 意思決定ロジック 
# ---------------------------------------------------------
async def run_master_agent_flow(session_id: str, user_message: str):
    """
    ReAct (Reason+Act) パターンによる自律エージェントループを実行します。
    """

    # リクエスト単位（セッション単位）でログキューを生成、複数クライアント間でのログ混線を防止する
    session_log_queue = asyncio.Queue()


    MAX_TURNS = 5  # 無限ループ防止のための最大ターン数

    # 1. セッション管理
    if session_id not in CHAT_SESSIONS:
        CHAT_SESSIONS[session_id] = []
    history = CHAT_SESSIONS[session_id]

    # ユーザーメッセージ追加
    history.append({"role": "user", "content": user_message})

    # 2. Master Prompt (思考の構造化と定義)
    system_instruction = """
    あなたはB2B顧客開拓の専門家です。
    ユーザー（売り手）の入力から、最適な「ターゲット企業（買い手）」を特定し（中国範囲内のみ）、スクリーニングを行うのが任務です。

    **【インタラクション・フロー】**
    1. ユーザーのニーズを分析します。
    回答する前に、以下の手順でJSONの `thought` フィールドに思考を出力してください：
        a. **Subject Analysis**: ユーザーは何を売っている企業か？（Supply）
        b. **Target Analysis**: それを必要とするのはどんな業種の企業か？（Demand）
        c. **Gap Analysis**: ターゲットを特定するための情報は十分か？
        - ユーザーが特定の「業界」や「製品」に言及した場合 -> **即座に** `consult_knowledge_base` を使用し、その業界のサプライチェーン、商流、主要プレイヤー情報を取得してターゲットの解像度を高める。
        - 地域が決まっていない -> ユーザーへ質問（`response_to_user`）
    2. 外部検索ツール（`run_qcc_tool`）を実行する前に、必ず `propose_screening_condition` を呼び出して、ユーザーに検索条件の提案・確認を行ってください。
    3. ユーザーから明確な承認（「確認」「OK」の発言、または確認ボタンの押下）が得られた場合のみ、`run_qcc_tool` を実行します。
    4. ユーザーから修正の指示があった場合は、パラメータを修正して再度 `propose_screening_condition` を呼び出してください。

    **【利用可能なアクション】**
    1. `consult_knowledge_base`: ユーザーが特定の業界・製品に言及した際に**最優先で**使用します。知識不足の補完だけでなく、サプライチェーン構造を正確に把握し、より精度の高いターゲット選定を行うために積極的に検索を行ってください。
    2. `propose_screening_condition`: 検索条件の提案。ターゲット画像が固まったら、まずこれを使用します。
       params: { "guidance_text": "...", "keywords": "...", "regions": [...] }
    3. `run_qcc_tool`: ユーザー承認後に実行するスクリーニング。
       **params (必須):**
       - `guidance_text`: **必須**。`propose_screening_condition` で提案した内容（ターゲット定義）をそのまま転記すること。これが空だと検索できません。
       - `keywords`: **必須**。提案したキーワード。
       - `regions`: **必須**。提案した地域リスト。
       - `reasoning`: **(必須)** なぜこの条件（キーワードや地域）を選定したのかの理由説明。**ここにRAGの検索結果に基づいた根拠と引用タグ（例: [report.pdf p.12]）を必ず含めること。**
    4. `response_to_user`: ユーザーに追加質問をする、または回答する。

    【ナレッジベース利用時の重要ルール: 引用の義務】
    ナレッジベース検索(`consult_knowledge_base`)の結果を利用して発言する場合は、必ず情報の出典元を明記してください。
    提供されるテキストには `[ファイル名 p.ページ番号]` という形式のタグが含まれています。
    回答文や提案理由の、該当する事実の直後にこのタグをそのまま付記してください。
    提供されたタグにページ番号が含まれていない場合（例: `[file.pdf]`）、決して勝手にページ番号を捏造しないでください。その場合は `[file.pdf]` とだけ記述してください。
    


    **【出力フォーマット】**
    必ず以下のJSON形式のみを出力してください。Markdownは不要です。

    例：業界への言及があるため、まずナレッジベースを確認する場合
    ```json
    {
        "thought": "ユーザーは「自動車ガラス」という特定の製品に言及している。ターゲット企業の解像度を高めるため、まずはナレッジベースで自動車ガラス業界のサプライチェーンや、主要な納入先（OEMメーカー等）を検索して確認する必要がある。",
        "action": "consult_knowledge_base",
        "params": {
            "query": "汽车玻璃 供应链 主要客户"
        }
    }
    ```


    
    または
    
    例: 条件提案時
    AI回答：
    ```json
    {
        "thought": "自動車ガラス業界のレポートによると、福耀ガラスが主要プレイヤーであるため、その周辺サプライヤーを狙うべきだ。",
        "action": "propose_screening_condition",
        "params": {
            "guidance_text": "ターゲット：自動車ガラス製造に関連するサプライヤーおよび加工業者",
            "regions": ["福建省", "上海市"],
            "keywords": "自動車ガラス、PVB膜、ケイ砂",
            "reasoning": "業界レポートによると、福耀ガラスの主要工場は福建省と上海に集中しています [AutoGlass_Report_2024.pdf p.15]。また、原材料としてPVB膜の需要が急増しているとの記述があります [Market_Analysis.xlsx]。"
        }
    }
    ```

    または
    
    ```json
    {
        "thought": "ターゲットは判明したが、地域が不明だ。ユーザーに聞く必要がある。",
        "action": "response_to_user",
        "params": {
            "text": "ターゲットとして自動車組立工場が考えられます。スクリーニングを行いたい「地域」（例：中国・広東省など）を教えていただけますか？"
        }
    }
    ```


    承認後のツール実行の場合
    ユーザー：「確認しました。開始してください。」
    AI回答：
    ```json
    {
        "thought": "ユーザーの承認が得られた。提案時の条件（guidance_text含む）をパラメータに設定してツールを実行する。",
        "action": "run_qcc_tool",
        "params": {
            "guidance_text": "ターゲット：広東省のガラス深加工企業および自動車部品メーカー",
            "regions": ["広東省"],
            "keywords": "ガラス加工、自動車部品"
        }
    }
    ```


    



    一方で、あなたは高度なデータアナリストでもあります。
    ユーザーから社内データに関する質問があった場合は、社内データベースの分析を行ってください。

    **【社内データベース情報 (PostgreSQL)】**
    そのような場合には、以下に示すテーブル構造およびサンプルデータを十分に理解したうえで、適切な SQL を作成してください。
    データは複数のテーブルに分かれている可能性があります。
    必要な情報は `JOIN` を使用して結合し取得すること。

    --- Database Schema Cache ---
    {db_schema_context}
    -----------------------------

    **【利用可能なアクション (search_internal_crm)】**
    ユーザーが社内データに関する質問をした場合は、このツールを使用してください。
    **params:**
    - `sql_query`: PostgreSQL互換の実行可能なSELECT文。Markdownのコードブロックは不要です。
    
    **重要: SQL生成のルール**
    1. 取得するカラムには、内容がわかりやすいエイリアス(AS)を付けてください（例: `company_name`, `deal_status`, `contact_person` など）。
    2. ユーザーの質問に答えるために必要なカラムのみを選択してください（`SELECT *` は避けること）。
    3. 複数のテーブルに同名のカラムがある場合は、必ずテーブル修飾子（例: `c.name`, `s.status`）を使用してください。

    **【出力フォーマット】**
    必ずJSON形式のみを出力してください。
    
    例: 「上海にある商談中の企業と担当者を教えて」
    ```json
    {{
        "thought": "ユーザーは上海の商談中企業を探している。companiesテーブルとsales_records, contactsテーブルをJOINし、企業名・担当者・ステータス等の主要情報を取得する。",
        "action": "search_internal_crm",
        "params": {{
            "sql_query": "SELECT c.name AS company_name, c.industry, s.status AS deal_status, ct.name AS contact_person, s.sales_amount, s.last_contact_date FROM companies c JOIN sales_records s ON c.id = s.company_id LEFT JOIN contacts ct ON c.id = ct.company_id WHERE c.region = '上海' AND s.status = '商談中'"
        }}
    }}
    ```

    回答テキストには、「*」の記号を含めないようにしてください。
    """

    yield f"data: [Thinking] エージェントが思考を開始しました...\n\n"

    current_turn = 0

    while current_turn < MAX_TURNS:
        current_turn += 1

        # --- LLM 呼び出し ---
        llm_response = await _call_master_llm(system_instruction, history)
        logger.info(f"Turn {current_turn} LLM Response: {llm_response}")

        # エージェント自身の発言として履歴に記録（思考・行動の文脈維持）
        history.append({"role": "assistant", "content": llm_response})

        # --- 解析 ---
        data = extract_json_from_text(llm_response)

        # JSON解析失敗時は、安全策としてテキストをそのまま返す
        if not data or "action" not in data:
            logger.warning("JSON Parse Failed or No Action. Treat as text.")
            safe_resp = llm_response.replace('\n', '\\n')
            yield f"data: [TEXT_RESPONSE]{safe_resp}\n\n"
            yield "data: ---END_OF_STREAM---\n\n"
            return

        # 思考内容をログ出力
        thought = data.get("thought", "")
        action = data.get("action")
        params = data.get("params", {})

        if thought:
            yield f"data: [Thinking] {thought}\n\n"

        # --- アクション分岐 ---

        # CASE 1: ユーザーへの返答
        if action == "response_to_user":
            resp_text = params.get("text", "")
            # 既にhistoryにはLLMの全出力が入っているが、整合性のため簡潔な応答も入れておくか検討可能
            # ここでは二重登録を防ぐため、Assistantの思考プロセスとしての履歴のみとする（仕様依存）
            
            # フィルタリング適用
            clean_text = sanitize_citations(resp_text)

            # フロントエンドへの表示用
            yield f"data: [TEXT_RESPONSE]{clean_text.replace('\n', '\\n')}\n\n"
            yield "data: ---END_OF_STREAM---\n\n"
            return  # ユーザー入力待ちへ

        # CASE 2: ナレッジベース検索
        elif action == "consult_knowledge_base":
            query = params.get("query", "")
            yield f"data: [STATUS_MSG]ナレッジベース検索中: {query}...\n\n"

            rag_result, hit_files, *_ = await asyncio.to_thread(query_knowledge_base, rag_index, query)
            
            if hit_files:
                for fname in hit_files:
                    # [RAG_HIT] を使うと App.jsx 側で success-note (少し強調されたスタイル) になります
                    msg = f"関連ドキュメントを検出しました【{fname}】"
                    yield f"data: [RAG_HIT]{msg}\n\n"
            else:
                 yield f"data: [STATUS_MSG]関連するドキュメントは見つかりませんでした。\n\n"
            
            
            logger.info(f"🔍 RAG Tool Output (Length: {len(rag_result)}): {rag_result[:3000]}...") 

            # 結果を履歴に追加（Tool Role）
            tool_msg = f"【Tool: Knowledge Base Result】\n{rag_result}"
            history.append({"role": "tool", "content": tool_msg})

            # 情報を保持したまま次のループへ（continue）
            continue

        # CASE 3: スクリーニングツール実行
        elif action == "run_qcc_tool":
            # 1. まず現在のパラメータを取得
            p_guidance = params.get("guidance_text", "")
            p_regions = params.get("regions", [])
            p_keywords = params.get("keywords", "")

            # ------------------------------------------------------------------
            # 履歴からの強制復元ロジック (History Hydration)
            # ------------------------------------------------------------------
            # パラメータが不足している場合、履歴から「提案データ」を復元する
            if not p_guidance:
                logger.info("⚠️ Action params empty. Retrieving from history...")
                for msg in reversed(history):
                    content = msg.get("content", "")
                    if msg.get("role") == "tool" and content.startswith("PROPOSAL_SAVED_DATA:"):
                        try:
                            # JSONを取り出す
                            json_str = content.replace("PROPOSAL_SAVED_DATA: ", "").strip()
                            data = json.loads(json_str)
                            
                            # 復元
                            p_guidance = data.get("guidance", "")
                            p_regions = data.get("regions", [])
                            p_keywords = data.get("keywords", "")
                            
                            logger.info(f"✅ Restored full params from history.")
                            break
                        except Exception as e:
                            logger.error(f"Failed to parse history data: {e}")

            # ------------------------------------------------------------------
            # Playwright用プロンプトの統合 (Prompt Injection)
            # Guidance, Regions, Keywords を一つのテキストに統合し、
            # Playwright側のLLMが「何を入力し、何を選択すべきか」を迷わないようにする
            # ------------------------------------------------------------------
            
            # キーワードの整形
            if isinstance(p_keywords, list):
                kw_str = "、".join(p_keywords)
            else:
                kw_str = str(p_keywords)

            # 地域の整形
            if isinstance(p_regions, list):
                reg_str = "、".join(p_regions)
            else:
                reg_str = str(p_regions)

            # 統合ガイダンステキストの作成
            # Playwright内のLLMはこのテキストを見て行動を決定します
            rich_guidance_text = f"""
            【スクリーニング目標】
            {p_guidance}

            【制約条件】
            以下の条件を参考にして、フィルタリングを行ってください：
            1. 検索キーワード: 「{kw_str}」
            2. 地域(省・エリア): 「{reg_str}」
            """

            logger.info(f"🚀 Starting Playwright with RICH GUIDANCE:\n{rich_guidance_text}")

            # 変数にセット
            playwright_test.LLM_GUIDANCE_TEXT = rich_guidance_text
            logger_instance = PlaywrightLogger(session_log_queue)

            # 実行タスク (以下変更なし)
            def _sync_run():
                if sys.platform == "win32":
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                try:
                    asyncio.run(
                        playwright_test.test_qcc_llm_interaction_with_playwright(logger_instance, rich_guidance_text))
                except Exception as e:
                    error_msg = traceback.format_exc()
                    logger_instance.log_to_frontend(f"❌ Error: {error_msg}")
                    logger.error(f"Playwright Error: {error_msg}")

            future = asyncio.to_thread(_sync_run)
            task = asyncio.create_task(future)

            final_report_content = ""

            # ストリーミングループ
            while True:
                queue_task = asyncio.create_task(session_log_queue.get())
                done, _ = await asyncio.wait({queue_task, task}, return_when=asyncio.FIRST_COMPLETED)

                if queue_task in done:
                    msg = queue_task.result()

                    # [FINAL_REPORT] タグを検出して保存
                    if "[FINAL_REPORT]" in msg:
                        final_report_content = msg.replace("data: ", "").replace("[FINAL_REPORT]", "").strip()

                    yield msg
                else:
                    queue_task.cancel()
                    break

            # 残りのログ排出
            while not session_log_queue.empty():
                try:
                    msg = session_log_queue.get_nowait()
                    if "[FINAL_REPORT]" in msg:
                        final_report_content = msg.replace("data: ", "").replace("[FINAL_REPORT]", "").strip()
                    yield msg
                except:
                    break

            # 履歴に保存
            if final_report_content:
                logger.info(f"Saving Tool Report to History ({len(final_report_content)} chars)")
                history.append({
                    "role": "tool",
                    "content": f"【Tool Execution Report】\nスクリーニングが完了しました。結果レポートは以下の通りです：\n{final_report_content}"
                })
            else:
                history.append({"role": "tool", "content": "スクリーニングが終了しましたが、レポートは生成されませんでした。エラーログを確認してください。"})

            yield "data: ---END_OF_STREAM---\n\n"
            return  # ツール実行完了で終了

        # CASE 4: 社内データベース検索
        elif action == "search_internal_crm":
            sql_query = params.get("sql_query", "")
            
            # --- スキーマ確認クエリかどうかの判定 ---
            # information_schema や pg_catalog を含むクエリは「内部確認」とみなす
            is_schema_check = "information_schema" in sql_query.lower() or "pg_catalog" in sql_query.lower()

            if is_schema_check:
                # ユーザーには「内部確認中」とだけ伝える
                yield f"data: [STATUS_MSG]データベースの構造を再確認しています...\n\n"
            else:
                # 通常の検索
                yield f"data: [STATUS_MSG]条件に基づいて社内データベースを検索中...\n\n"
            
            yield f"data: [Thinking] Database Querying...\n\n"

            # SQL実行
            db_results = await execute_raw_sql(sql_query)

            # エラーハンドリング
            if isinstance(db_results, dict) and "error" in db_results:
                error_msg = db_results["error"]
                tool_msg = f"【System Error】SQL Execution Failed:\n{error_msg}\nPlease correct your SQL and try again."
                history.append({"role": "tool", "content": tool_msg})
                continue

            # 成功時の処理
            if db_results:
                # --- ここで分岐: スキーマ確認ならフロントエンドには表示しない ---
                if is_schema_check:
                    # LLMには結果を見せる（学習させるため）が、ユーザーには見せない
                    result_preview = json.dumps(db_results[:10], ensure_ascii=False, default=str)
                    tool_msg = f"【System Info】Table Schema/Structure:\n{result_preview}\n(User did not see this. Now please construct the correct SQL for the user's request.)"
                    
                    # 履歴に追加するだけ
                    history.append({"role": "tool", "content": tool_msg})
                    logger.info("Schema check executed. Result hidden from frontend.")
                    continue  # continueして、LLMに次の正しいSQLを作らせる

                # --- 通常のデータ検索の場合 ---

                card_payload = json.dumps(db_results, ensure_ascii=False, default=str)
                yield f"data: [DB_CARD_DATA]{card_payload}\n\n"
                
                # LLM用コンテキスト
                result_count = len(db_results)
                preview_data = db_results[:5]
                tool_msg = f"""【Tool Result】
SQL Query executed successfully.
Total Records: {result_count}
First 5 rows preview:
{json.dumps(preview_data, ensure_ascii=False, default=str)}
"""
            else:
                tool_msg = f"【Tool Result】Query executed successfully but returned 0 records."

            history.append({"role": "tool", "content": tool_msg})
            continue
        # CASE 5: スクリーニング条件の提案 
        elif action == "propose_screening_condition":
            # パラメータの抽出
            p_guidance = params.get("guidance_text", "")
            p_regions = params.get("regions", [])
            p_keywords = params.get("keywords", "")
            p_reasoning = params.get("reasoning", "")
            
            # キーワードがリストの場合は文字列に結合
            if isinstance(p_keywords, list):
                p_keywords = "、".join(p_keywords)

            # 1. フロントエンド用データペイロードの作成
            proposal_data = {
                "guidance": p_guidance,
                "regions": p_regions,
                "keywords": p_keywords
            }
            

            payload = json.dumps(proposal_data, ensure_ascii=False)
            
            # 2. フロントエンドへ提案カードデータを送信
            yield f"data: [PROPOSAL_DATA]{payload}\n\n"

            # 3. 履歴に記録し、LLMにはユーザーの応答を待つよう指示
            tool_msg_content = f"PROPOSAL_SAVED_DATA: {payload}" 
            history.append({"role": "tool", "content": tool_msg_content})
            follow_up_text = (
                f"{p_reasoning}\n\n"
                "--- \n"
                "上記に基づき、ターゲット条件案を作成しました。\n"
                "条件に問題がなければ「検索開始」、修正が必要な場合は指示を入力してください。"
            )
            # フィルタリング適用
            clean_follow_up = sanitize_citations(follow_up_text)
            
            yield f"data: [TEXT_RESPONSE]{clean_follow_up.replace('\n', '\\n')}\n\n"
            
            # LLMの履歴にも自身が発言したこととして記録（一貫性維持のため）
            history.append({"role": "assistant", "content": clean_follow_up})
            # ここで一旦ストリームを終了し、ユーザーの入力を待つ
            yield "data: ---END_OF_STREAM---\n\n"
            return

    # ループ上限到達
    yield f"data: [TEXT_RESPONSE]処理が複雑すぎるため、一旦停止しました。条件を絞って再度入力してください。\n\n"
    yield "data: ---END_OF_STREAM---\n\n"


# --- API エンドポイント ---

@app.post("/chat")
async def chat_endpoint(request: Request):
    """
    チャットインターフェース
    Request JSON: { "message": "...", "session_id": "..." }
    """
    data = await request.json()
    user_message = data.get("message", "")
    session_id = data.get("session_id")

    if not session_id:
        session_id = str(uuid.uuid4())

    if not user_message:
        raise HTTPException(status_code=400, detail="Message is empty")

    return StreamingResponse(
        run_master_agent_flow(session_id, user_message),
        media_type="text/event-stream"
    )

@app.get("/")
async def root():
    return {"message": "チャットエージェントのバックエンドが稼働中です。"}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)