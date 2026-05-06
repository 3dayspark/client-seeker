

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

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import requests  # Gemini API用
from openai import OpenAI  # ModelScope API用

# 独自のRAGユーティリティをインポート
from rag_utils import build_or_load_index, query_knowledge_base, get_all_indexed_filenames
# データベース接続をインポート
from database import (
    get_database_schema_info, 
    execute_raw_sql, 
    get_table_schema_sync,     
    search_column_values,      
    vectorize_database,        
    search_table_semantically  
)

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
    "https://d3h1t8qwipl5h7.cloudfront.net",
    "http://client-finder-frontend-bucket.s3-website.us-east-2.amazonaws.com"
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


GEMINI_API_KEYS = playwright_test.GEMINI_API_KEYS
GEMINI_API_URL = playwright_test.GEMINI_API_URL
USE_GEMINI_AS_LLM = False
MODEL_CANDIDATES =[
    "deepseek-ai/DeepSeek-V4-Flash",
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "Qwen/Qwen3-235B-A22B",
    "MiniMax/MiniMax-M2.7"
]


# --- RAG制御用グローバルスイッチ ---

# True: LLMがファイル名から推測して絞り込む（高速だが漏れのリスクあり）
# False: 全ファイルを対象に検索する（低速だが確実、Recall重視）
ENABLE_AGENTIC_FILE_SELECTION = True 

# True: LLMが検索結果を読んで「関連なし」と判断したら捨てる（Precision重視だが誤判定リスクあり）
# False: 検索でヒットしたものは全てコンテキストとして採用する（Recall重視、幻覚防止）
ENABLE_AGENTIC_RESULT_VERIFICATION = True

USE_LLM_FOR_SUGGESTIONS = True

# --- グローバル変数 ---
modelscope_client = None
gemini_api_key_pool = None
gemini_base_headers = {"Content-Type": "application/json"}
rag_index = None
VALID_FILENAMES = set() 
CURRENT_MODEL_INDEX = 0

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
    末尾の余分な「}」やゴミ文字にも対応します。
    """
    try:
        # 1. Markdownのコードブロック記法を除去
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```', '', text)
        text = text.strip()

        # 2. 最初に見つかった { を探す (rfind は使わない)
        start_idx = text.find('{')
        if start_idx == -1:
            return None

        # { から最後までの文字列を切り出す
        json_str = text[start_idx:]

        # 3. JSON文字列内の「不正な改行」を「\\n」に置換する処理
        new_chars =[]
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

        # 4. raw_decodeを使用して最初の有効なJSONオブジェクトを抽出
        # これにより、末尾に多重の「}」やテキストが混ざっていても無視してパースできます。
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(json_str_clean)
        
        return obj

    except json.JSONDecodeError as e:
        logger.warning(f"JSON Parse Error: {e} | Raw: {text[:100]}...")
        # 最後の手段1：制御文字を削除してraw_decode
        try:
            simple_clean = re.sub(r'[\x00-\x1f]', ' ', text[start_idx:])
            obj, _ = json.JSONDecoder().raw_decode(simple_clean)
            return obj
        except:
            pass
            
        # 最後の手段2：強引に後ろから「}」を削りながらトライ
        try:
            temp_str = json_str_clean
            while temp_str.rfind('}') != -1:
                # 最後の } の位置で切り取る
                temp_str = temp_str[:temp_str.rfind('}') + 1]
                try:
                    return json.loads(temp_str)
                except:
                    # 失敗したら最後の } を削って次へ
                    temp_str = temp_str[:-1]
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


# ---------------------------------------------------------
# ファイル選択用 LLM 呼び出し
# ---------------------------------------------------------
async def _select_relevant_files_with_llm(query: str, all_filenames: list) -> list[str]:
    if not all_filenames:
        return []
    
    files_str = "\n".join([f"- {name}" for name in all_filenames])
    
    prompt = f"""
    あなたはナレッジベースの司書です。
    ユーザーの質問に答えるために、どのドキュメントを参照すべきか判断してください。

    【ユーザーの質問】
    {query}

    【利用可能なファイル一覧】
    {files_str}

    【指示】
    - 質問に関連する可能性が高いファイル名をリストアップしてください。
    - ファイル名から判断して、全く関係のないファイルは除外してください。
    - 迷った場合は、そのファイルを含めてください。
    - 結果は必ず以下のJSONフォーマットのみで出力してください。

    ```json
    {{
        "selected_files": ["file1.pdf", "file2.xlsx"]
    }}
    ```
    """

    try:
        response = await _call_master_llm(prompt,[])
        data = extract_json_from_text(response)
        if data and "selected_files" in data:
            selected = data["selected_files"]
            logger.info(f"📂 LLM Selected Files: {selected}")
            
            valid_selected =[f for f in selected if f in VALID_FILENAMES]
            if not valid_selected and selected:
                 for s in selected:
                     for v in VALID_FILENAMES:
                         if s in v or v in s:
                             valid_selected.append(v)
            return list(set(valid_selected))
    except Exception as e:
        logger.error(f"🚨 ファイル自動選定LLM失敗 (安全のため全件検索にフォールバック): {e}")

    return[]


async def _verify_rag_result(query: str, rag_text: str) -> dict:
    if not rag_text or "関連ドキュメント" in rag_text and "見つかりませんでした" in rag_text:
        return {"is_relevant": False, "bad_files":[]}

    prompt = f"""
    あなたは検索結果の品質評価者です。
    ユーザーの質問に対して、RAG（検索システム）が返したテキストが「答えを含んでいる」か、あるいは「役に立たない/無関係」かを判定してください。

    【ユーザーの質問】
    {query}

    【RAG検索結果】
    {rag_text}

    【判定基準】
    1. 検索結果が質問の意図（具体的な製品、数値、企業名など）に答えている、または関連する手がかりを含んでいる場合は `is_relevant: true` としてください。
    2. 検索結果が具体的な質問内容と無関係な場合は `is_relevant: false` としてください。
    3. `is_relevant: false` の場合、そのノイズ情報の出典元ファイル名（例: [file.pdf]）を抽出して `bad_files` リストに入れてください。

    出力は以下のJSON形式のみです：
    ```json
    {{
        "is_relevant": false,
        "bad_files": ["全球产业链百科全书.pdf"]
    }}
    ```
    """

    try:
        resp = await _call_master_llm(prompt,[])
        data = extract_json_from_text(resp)
        if data:
            return data
    except Exception as e:
        logger.error(f"🚨 RAG検証LLM失敗 (安全のため結果をそのまま採用します): {e}")
    
    # 失敗した場合は安全のため、全て「関連あり」として通す
    return {"is_relevant": True, "bad_files":[]}


# --- 補助関数: LLM 呼び出し (Master Brain) ---
async def _call_master_llm(prompt: str, history: List[Dict[str, str]]) -> str:
    """
    LLM を呼び出して応答を生成します。履歴をプロンプトに統合します。
    エラーまたは空の応答が発生した場合は、自動的に候補モデルリスト(MODEL_CANDIDATES)から次のモデルに切り替えてリトライします。
    """
    global CURRENT_MODEL_INDEX
    
    # 1. コンテキスト文字列の構築
    history_text = ""
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
            if "[判断根拠: " in readable_content:
                readable_content = readable_content.replace("\n", "]\n")
            history_text += f"System (Tool Execution Result): \n{readable_content}\n"

    full_prompt = f"""
    {prompt}
    
    --- チャット履歴 (Chat History) ---
    {history_text}
    
    Assistant:
    """

    last_error = None
    # 2. 自動モデルフォールバック（失敗した時だけ次のモデルを試す）
    for attempt in range(len(MODEL_CANDIDATES)):
        idx = (CURRENT_MODEL_INDEX + attempt) % len(MODEL_CANDIDATES)
        target_model = MODEL_CANDIDATES[idx]

        # ModelScope の呼び出し
        if not USE_GEMINI_AS_LLM and modelscope_client:
            try:
                response = await asyncio.to_thread(
                    modelscope_client.chat.completions.create,
                    model=target_model, 
                    messages=[{'role': 'user', 'content': full_prompt}],
                    stream=False,
                    extra_body={"enable_thinking": False}
                )
                if response and hasattr(response, 'choices') and response.choices:
                    CURRENT_MODEL_INDEX = idx  # 成功したらグローバルインデックスを更新
                    return response.choices[0].message.content.strip()
                else:
                    raise ValueError(f"API Returned an empty structure (choices=None) for {target_model}")
            except Exception as e:
                logger.warning(f"⚠️ Model [{target_model}] failed: {e}. Trying next...")
                last_error = e
                continue

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
                CURRENT_MODEL_INDEX = idx
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
            except Exception as e:
                logger.warning(f"⚠️ Gemini error: {e}. Trying next...")
                last_error = e
                continue

    # 3. すべてのモデルが失敗した場合のみエラーを投げる
    raise RuntimeError(f"All LLM candidate models failed. Last error: {last_error}")



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

    asyncio.create_task(vectorize_database())


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
        **params:**
        - `text`: **必須**。回答テキスト。

    【ナレッジベース利用時の重要ルール: 引用の義務】
    ナレッジベース検索(`consult_knowledge_base`)の結果を利用して発言する場合は、必ず情報の出典元を明記してください。
    提供されるテキストには `[ファイル名 p.ページ番号]` という形式のタグが含まれています。
     **ファイル名の省略は厳禁です。** 
       ❌ 悪い例: `[p.12]` や `[page 12]`
       ✅ 正しい例: `[report_2024.pdf p.12]`
    回答文や提案理由の、該当する事実の直後にこのタグをそのまま付記してください。
    提供されたタグにページ番号が含まれていない場合（例: `[file.pdf]`）、`[file.pdf]` とだけ記述してください。
    

    **【出力フォーマット】**
    必ず以下のJSON形式のみを出力してください。Markdownは不要です。

    例：業界への言及があるため、まずナレッジベースを確認する場合
    ```json
    {
        "thought": "ユーザーは「自動車ガラス」という特定の製品に言及している。ターゲット企業の解像度を高めるため、まずはナレッジベースで自動車ガラス業界のサプライチェーンや、主要な納入先（OEMメーカー等）を検索して確認する必要がある。",
        "action": "consult_knowledge_base",
        "params": {
            "query": "汽车玻璃在它的供应链中可能有哪些主要客户？"
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
    そのような場合には、以下に示すテーブル構造およびサンプルデータを十分に理解したうえで、データベースの分析を行います。
    データは複数のテーブルに分かれている可能性があります。
    必要な情報は `JOIN` を使用して結合し取得すること。

    --- Database Schema Cache ---
    {db_schema_context}
    -----------------------------

    **【利用可能なアクション】**
    1.`semantic_search`: 
       SQLの「完全一致」や「ILIKE」では検索しきれない、曖昧な条件や定性的な表現（例：「このプロファイルに当てはまりそうな顧客」「環境に優しい企業」「物流関連の担当者」）で検索したい場合に使用します。
       システムは自動的にベクトル類似度計算を行います。
       **params:**
       - `table_name`: (str) 必填。検索対象のテーブル名 (例: 'companies', 'contacts')。
       - `query_text`: (str) 必填。自然言語による検索クエリ。
    2.`inspect_database_values`: SQLを書く前に、カラムに入っている具体的な値（文字列）を確認するためのツール。
       **params:**
       - `table_name`: (str) 確認したいテーブル名 (例: 'companies')
       - `column_name`: (str) 確認したいカラム名 (例: 'industry', 'status')
       - `keyword`: (str, optional) 検索したいキーワード。指定しない場合は頻出する値を返します。
    3. `search_internal_crm`: ユーザーが社内データに関する質問をした場合は、このツールを使用してください。
     (注意: 曖昧な検索をする際は、必ず `ILIKE` 演算子を使用するか、事前に `inspect_database_values` で値を確認してください)
        **params:**
        - `sql_query`: PostgreSQL互換の実行可能なSELECT文。Markdownのコードブロックは不要です。
    
    

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

    yield ":\n\n"  # SSE keep-alive
    yield f"data: [Thinking] 思考中です…\n\n"

    current_turn = 0
    has_proposed = False
    has_searched_db = False
    current_model_index = 0  

    while current_turn < MAX_TURNS:
        current_turn += 1

        # --- LLM 呼び出し ---
        try:
            llm_response = await _call_master_llm(system_instruction, history)
        except Exception as e:
            logger.error(f"Master Agent LLM Call Failed completely: {e}")
            yield f"data: [TEXT_RESPONSE]申し訳ありません。現在すべてのAIモデルが一時的に制限されているか、利用枠に達したため回答を生成できません。\n\n"
            yield "data: ---END_OF_STREAM---\n\n"
            return

        # エージェント自身の発言として履歴に記録
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


        # ストリームを閉じて終了する前に、動的プロンプトを判定して送るヘルパー関数
        async def get_suggested_prompts(text_response=""):
            
            if USE_LLM_FOR_SUGGESTIONS:
                prompt_text = """
                あなたはユーザーの次の発言を予測するアシスタントです。
                これまでの会話履歴と、あなたがたった今出力した最新の回答を踏まえて、ユーザーが次に尋ねる可能性が高い質問や指示を3つ提案してください。
                提案は以下の条件を満たす必要があります：
                1. ユーザーの意図に沿ったものであり、B2B顧客開拓、スクリーニング、社内DB検索というエージェントの機能に繋がるものであること。
                2. 「label」は10文字以内の短い見出し、「text」は具体的な発言内容（30文字程度）にすること。
                
                また、直前の回答が以下のいずれかに該当する場合は、通常の質問提案は行わず、「別の顧客像を生成する」「現在フォロー中の顧客を確認する」など、他の機能を利用する提案のみを行ってください。ユーザーに代わって問題解決のための追加質問を提案する必要はありません。
                    - 申し訳ございません。クラウドサーバーが中国以外のIPのため、中国の法規制により、現在qcc.comの中国企業検索ページをご利用いただけません。
                    - エラーが発生しました

                
                必ず以下のJSON形式のみで出力してください。
                ```json
                {
                    "suggestions":[
                        {"label": "連絡先を教えて", "text": "この会社の連絡先を教えていただけますか？"},
                        {"label": "...", "text": "..."}
                    ]
                }
                ```
                """
                try:
                    resp = await _call_master_llm(prompt_text, history)
                    data = extract_json_from_text(resp)
                    if data and "suggestions" in data and len(data["suggestions"]) >= 1:
                        return data["suggestions"][:3] 
                except Exception as e:
                    logger.error(f"LLM Suggestion Error, fallback to rule-based: {e}")
            
            
            db_keywords =["連絡先", "商談", "携帯", "抽出", "企業", "データ", "件見つかり", "検索結果", "該当する"]
            proposal_keywords =["ターゲット", "条件案", "プロファイル", "提案", "画像", "定義", "スクリーニング"]
            ask_keywords =["地域", "エリア", "業種", "規模", "売上", "どのよう", "教えて", "詳細", "希望", "いかが", "条件"]
            
            is_db_context = has_searched_db or any(kw in text_response for kw in db_keywords)
            is_proposal_context = has_proposed or any(kw in text_response for kw in proposal_keywords)
            is_asking = any(kw in text_response for kw in ask_keywords) or "?" in text_response or "？" in text_response

            if is_proposal_context and not is_asking:
                return[
                    {"label": "類似の既存顧客を検索", "text": "この顧客プロファイルに最も当てはまりそうな、データベース内の既存顧客を教えていただけますか？"},
                    {"label": "DB内の業界から顧客を検索", "text": "この顧客プロファイルに一番当てはまりそうな、当社の既存顧客を教えてください。（まずデータベース内の顧客の業界をすべて確認してください）"}
                ]
            elif is_db_context and not is_asking:
                return[
                    {"label": "連絡先を教えて", "text": "この会社の連絡先を教えていただけますか？"},
                    {"label": "携帯番号ありのみ抽出", "text": "今の条件で、携帯番号のデータが入っている企業だけを抽出してください。"},
                    {"label": "9000万円超＆2024年以降", "text": "商談金額9,000万円超かつ最終接触日が2024年以降の顧客を抽出してください。"}
                ]
            else:
                return[
                    {"label": "南部・優良自動車メーカー", "text": "中国南部地域に位置し、経営状態が良好で生産規模が大きく、高年商かつ信用リスクの低い自動車メーカー"},
                    {"label": "上海・深センの资金力", "text": "エリアは主に上海と深センです。経営状態が良好で、資金力が豊富な企業を希望します。業界については、先ほどの例の通りでお願いします。"}
                ]



        # --- アクション分岐 ---

        # CASE 1: ユーザーへの返答
        if action == "response_to_user":
            
            resp_text = (
                params.get("text")
                or params.get("message")
                or params.get("response")
                or params.get("content")
                or data.get("text")
                or data.get("message")
                or data.get("response")
                or data.get("content")
                or ""
            )
            
            if not resp_text:
                resp_text = str(params) if params else "申し訳ありません。回答のフォーマットに一時的な異常が発生しました。"
            
            # フィルタリング適用
            clean_text = sanitize_citations(resp_text)

            # フロントエンドへの表示用
            yield f"data: [TEXT_RESPONSE]{clean_text.replace('\n', '\\n')}\n\n"
            
            # ストリームを閉じる直前にサジェストプロンプトを送信
            suggest_list = await get_suggested_prompts(clean_text)
            suggests = json.dumps(suggest_list, ensure_ascii=False)
            yield f"data: [SUGGEST_PROMPTS]{suggests}\n\n"
            
            
            yield "data: ---END_OF_STREAM---\n\n"
            return  # ユーザー入力待ちへ

        # CASE 2: ナレッジベース検索
        elif action == "consult_knowledge_base":
            query = params.get("query", "")
            
            # --- 1. 検索対象ファイルの決定 (Targeting) ---
            target_files = None # デフォルトは None (全検索)

            if ENABLE_AGENTIC_FILE_SELECTION:
                yield f"data: [Thinking] 質問に関連するドキュメントを選定しています (Agentic Selection)...\n\n"
                all_files_list = list(VALID_FILENAMES)
                
                # LLMによる推論
                selected_files = await _select_relevant_files_with_llm(query, all_files_list)
                
                if selected_files:
                    target_files = selected_files
                    yield f"data: [STATUS_MSG]対象ファイルを {len(target_files)} 件に絞り込みました。\n\n"
                else:
                    # 選択に失敗した場合は、安全のため全検索にフォールバック
                    yield f"data: [STATUS_MSG]関連ファイルの特定に迷ったため、全ファイルを対象に検索します。\n\n"
                    target_files = None
            else:
                # 安全重視：最初から全件検索
                yield f"data: [Thinking] ナレッジベース全体を対象に、確実な検索を実行します (Global Search)...\n\n"
                target_files = None

            
            # --- 2. 検索実行と検証 (Execution & Verification) ---
            final_rag_result = ""
            final_hit_files = []

            
            loop = asyncio.get_running_loop()
            def rag_progress_callback(msg: str):
                try:
                    loop.call_soon_threadsafe(session_log_queue.put_nowait, f"data: [Thinking] {msg}\n\n")
                except Exception:
                    pass
            
            
            
            # A. 検証モード (Agentic Loop)
            if ENABLE_AGENTIC_RESULT_VERIFICATION:
                # 既存の「検索 -> 検証 -> 除外 -> 再検索」ループロジック
                max_retries = 2  
                attempt = 0
                excluded_files = set()
                
                # target_files が None の場合は全ファイルリストを展開して管理する必要がある
                current_search_scope = target_files if target_files else list(VALID_FILENAMES)

                while attempt <= max_retries:
                    attempt += 1
                    
                    # 除外ファイルをフィルタリング
                    effective_search_files = [f for f in current_search_scope if f not in excluded_files]
                    
                    if not effective_search_files:
                        yield f"data: [STATUS_MSG]検索候補がすべて除外されました。\n\n"
                        final_rag_result = "（有効な情報が見つかりませんでした）"
                        break

                    yield f"data: [STATUS_MSG]現在検索中…{attempt}回目の試行です。お待ちください。\n\n"
                    
                    # 検索実行
                    def sync_rag_run():
                        return query_knowledge_base(
                            rag_index, 
                            query, 
                            target_filenames=effective_search_files if ENABLE_AGENTIC_FILE_SELECTION else None,
                            progress_callback=rag_progress_callback
                        )
                    
                    rag_task = asyncio.create_task(asyncio.to_thread(sync_rag_run))
                    queue_task = asyncio.create_task(session_log_queue.get())

                    while True:
                        done, pending = await asyncio.wait(
                            {queue_task, rag_task}, 
                            return_when=asyncio.FIRST_COMPLETED, 
                            timeout=15.0 
                        )
                        
                        if not done:
                            yield ": keep-alive heartbeat\n\n"
                            continue
                            
                        if queue_task in done:
                            yield queue_task.result()
                            queue_task = asyncio.create_task(session_log_queue.get())
                            
                        if rag_task in done:
                            if queue_task in pending:
                                queue_task.cancel()
                            break
                    
                    
                    while not session_log_queue.empty():
                        yield session_log_queue.get_nowait()
                        
                    rag_result, hit_files, *_ = rag_task.result()

                    # LLMによる検証
                    verification = await _verify_rag_result(query, rag_result)
                    
                    if verification and verification.get("is_relevant") is True:
                        # 承認
                        final_rag_result = rag_result
                        final_hit_files = hit_files
                        logger.info("✅ RAG result verified as relevant.")
                        break
                    else:
                        # 拒否 -> 除外リストへ
                        bad_files = verification.get("bad_files", []) if verification else []
                        if not bad_files: bad_files = hit_files 

                        logger.warning(f"🗑️ [Agentic Verify] REJECTED. Removing noise files: {bad_files}")

                        for bf in bad_files:
                            cleaned_bf = bf.strip()
                            for valid_f in VALID_FILENAMES:
                                if valid_f in cleaned_bf or cleaned_bf in valid_f:
                                    excluded_files.add(valid_f)
                        
              
                        yield f"data: [Thinking] 検索結果に関連性が低い情報が含まれていたため、ノイズを除去して再検索します...\n\n"
            
            # B. 標準モード (Standard Global Search) 
            else:
                def sync_rag_run():
                    return query_knowledge_base(
                        rag_index, 
                        query, 
                        target_filenames=target_files,
                        progress_callback=rag_progress_callback
                    )
                    
                rag_task = asyncio.create_task(asyncio.to_thread(sync_rag_run))
                queue_task = asyncio.create_task(session_log_queue.get())

                while True:
                    done, pending = await asyncio.wait(
                        {queue_task, rag_task}, 
                        return_when=asyncio.FIRST_COMPLETED, 
                        timeout=15.0  
                    )
                    
                    if not done:
                        yield ": keep-alive heartbeat\n\n"
                        continue
                        
                    if queue_task in done:
                        yield queue_task.result()
                        queue_task = asyncio.create_task(session_log_queue.get())
                        
                    if rag_task in done:
                        if queue_task in pending:
                            queue_task.cancel()
                        break
                
                while not session_log_queue.empty():
                    yield session_log_queue.get_nowait()
                    
                rag_result, hit_files, *_ = rag_task.result()
                final_rag_result = rag_result
                final_hit_files = hit_files

            # --- 3. 結果の出力と履歴保存 (Output) ---
            if final_hit_files:
                for fname in final_hit_files:
                    msg = f"参照ファイル: {fname}"
                    yield f"data: [RAG_HIT]{msg}\n\n"
            else:
                 yield f"data: [STATUS_MSG]ドキュメント内に関連情報が見つかりませんでした。\n\n"
            
            # 履歴に追加
            tool_msg = f"【Tool: Knowledge Base Result】\nResult: {final_rag_result}"
            history.append({"role": "tool", "content": tool_msg})
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


            # ストリームを閉じる直前にサジェストプロンプトを送信
            suggest_list = await get_suggested_prompts(final_report_content)
            suggests = json.dumps(suggest_list, ensure_ascii=False)
            yield f"data: [SUGGEST_PROMPTS]{suggests}\n\n"

            yield "data: ---END_OF_STREAM---\n\n"
            return  # ツール実行完了で終了

       # CASE 4: データベースの値確認
        elif action == "inspect_database_values":
            t_name = params.get("table_name")
            c_name = params.get("column_name")
            kw = params.get("keyword")
            
            yield f"data: [STATUS_MSG]データベース内の「{c_name}」に関する登録状況を確認中...\n\n"
            
            # database.py からインポートした関数を呼び出す
            from database import search_column_values 
            val_result = await search_column_values(t_name, c_name, kw)
            
            tool_msg = f"【Tool: Value Inspection Result】\n{json.dumps(val_result, ensure_ascii=False)}\n\n" \
                       f"これらはDBに実在する値です。ユーザーの意図に最も近いものを選択し、次のステップで正確なSQLを作成してください。"
            
            history.append({"role": "tool", "content": tool_msg})
            
            # 結果を表示しつつ、ループを継続してAgentに次の判断(SQL作成)をさせる
            yield f"data: [Thinking] 値を確認しました。これに基づいてSQLを作成します...\n\n"
            continue
   
        # CASE 5: semantic_search
        elif action == "semantic_search":
            has_searched_db = True 
            table_name = params.get("table_name", "companies")
            query_text = params.get("query_text", "")
            
            yield f"data: [STATUS_MSG]「{table_name}」テーブルから「{query_text}」をセマンティック検索中...\n\n"
            
            # database.py からインポートした関数を使用
            results = await search_table_semantically(table_name, query_text)
            
            if results:
                card_payload = json.dumps(results, ensure_ascii=False, default=str)
                yield f"data: [DB_CARD_DATA]{card_payload}\n\n"
                
                tool_msg = f"【Tool: Semantic Search Result ({table_name})】\nFound {len(results)} matches:\n{json.dumps(results, ensure_ascii=False, default=str)}\n"
            else:
                tool_msg = f"【Tool Result】No semantic matches found in table '{table_name}'."
            
            history.append({"role": "tool", "content": tool_msg})
            continue
        
        
        # CASE 6: 社内データベース検索
        elif action == "search_internal_crm":
            has_searched_db = True
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
            
            yield f"data: [Thinking] データベース検索中…\n\n"

            # SQL実行
            db_results = await execute_raw_sql(sql_query)

            #  エラーハンドリングの強化 
            if isinstance(db_results, dict) and "error" in db_results:
                error_msg = db_results["error"]
                
                # エラーメッセージからテーブル名を推測し、正しいスキーマを添付する
                # LLMが幻覚（存在しないカラム）を見ている場合の特効薬
                hint_schema = ""
                
                # SQL内のテーブル名を簡易抽出（正規表現で "FROM table" や "JOIN table" を探す）
                found_tables = re.findall(r'(?:FROM|JOIN)\s+([a-zA-Z0-9_]+)', sql_query, re.IGNORECASE)
                if found_tables:
                    hint_schema = "\n\n【Hint: Correct Schema Definition】\n"
                    for t in set(found_tables):
                        hint_schema += get_table_schema_sync(t) + "\n"

                tool_msg = (
                    f"【System Error】SQL Execution Failed:\n{error_msg}\n"
                    f"{hint_schema}\n"
                    f"Please correct your SQL based on the schema above and try again."
                )
                
                logger.warning(f"SQL Failed. Providing hint to LLM: {hint_schema[:100]}...")
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
        # CASE 7: スクリーニング条件の提案 
        elif action == "propose_screening_condition":
            has_proposed = True
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
            
            # ストリームを閉じる直前にサジェストプロンプトを送信
            suggest_list = await get_suggested_prompts(clean_follow_up) 
            suggests = json.dumps(suggest_list, ensure_ascii=False)
            yield f"data: [SUGGEST_PROMPTS]{suggests}\n\n"
            
            # ここで一旦ストリームを終了し、ユーザーの入力を待つ
            yield "data: ---END_OF_STREAM---\n\n"
            return

    # ループ上限到達
    yield f"data: [TEXT_RESPONSE]処理が複雑すぎるため、一旦停止しました。条件を絞って再度入力してください。\n\n"
    yield "data: ---END_OF_STREAM---\n\n"


# --- API エンドポイント ---

SECRET_PASSWORD = os.getenv("APP_PASSWORD", "local_dev_password")


@app.get("/verify-password")
async def verify_password(x_api_key: str = Header(None)):
    if x_api_key != SECRET_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid Password")
    return {"status": "ok"}


@app.post("/chat")
async def chat_endpoint(request: Request, x_api_key: str = Header(None)):
    

    if x_api_key != SECRET_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid Password")
    data = await request.json()
    user_message = data.get("message", "")
    session_id = data.get("session_id")

    if not session_id:
        session_id = str(uuid.uuid4())

    if not user_message:
        raise HTTPException(status_code=400, detail="Message is empty")


    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no"  
    }



    return StreamingResponse(
        run_master_agent_flow(session_id, user_message),
        media_type="text/event-stream",
        headers=headers
    )

@app.get("/")
async def root():
    return {"message": "チャットエージェントのバックエンドが稼働中です。"}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)