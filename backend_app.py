

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
from rag_utils import build_or_load_index, query_knowledge_base

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
USE_GEMINI_AS_LLM = True

# --- グローバル変数 ---
modelscope_client = None
gemini_api_key_pool = None
gemini_base_headers = {"Content-Type": "application/json"}
rag_index = None

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
    LLMの回答からJSONブロックを抽出し、構文エラー（特に改行コード）を自動修復してパースします。
    """
    try:
        # 1. Markdownのコードブロック記法を除去
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```', '', text)

        # 2. 最初に見つかった { ... } のペアを探す (最長一致)
        match = re.search(r'(\{[\s\S]*\})', text)
        if match:
            json_str = match.group(1)
        else:
            json_str = text

        # 3. JSON文字列内の改行コード処理
        # LLMがJSON文字列内部で改行し、パースエラーになるケースを防ぐため、
        # ダブルクォート内の改行を \n に置換する。
        def replace_newlines_in_quotes(m):
            content = m.group(0)
            return content.replace('\n', '\\n')

        # ダブルクォート内の内容にマッチさせ、改行をエスケープ
        json_str_clean = re.sub(r'"((?:[^"\\]|\\.)*)"', replace_newlines_in_quotes, json_str, flags=re.DOTALL)

        return json.loads(json_str_clean)

    except json.JSONDecodeError as e:
        logger.warning(f"JSON Parse Error: {e} | Raw: {text}")
        return None
    except Exception as e:
        logger.error(f"Extract Error: {e}")
        return None


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


# --- ログキューシステム ---
log_queue = asyncio.Queue()

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


# ---------------------------------------------------------
# コア: Master Agent 意思決定ロジック (ReActループ版)
# ---------------------------------------------------------
async def run_master_agent_flow(session_id: str, user_message: str):
    """
    ReAct (Reason+Act) パターンによる自律エージェントループを実行します。
    """
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

    **【思考プロセス】**
    回答する前に、以下の手順でJSONの `thought` フィールドに思考を出力してください：
    1. **Subject Analysis**: ユーザーは何を売っている企業か？（Supply）
    2. **Target Analysis**: それを必要とするのはどんな業種の企業か？（Demand）
    3. **Gap Analysis**: ターゲットを検索するために「地域（例：上海、广东）」や「具体的な業種キーワード」は揃っているか？
       - 業界知識が不足している -> `consult_knowledge_base`
       - 情報は揃った -> `run_qcc_tool`
       - 地域が決まっていない -> ユーザーへ質問（`response_to_user`）

    **【利用可能なアクション】**
    1. `consult_knowledge_base`: 業界知識（サプライチェーン、関連業種）を検索。
    2. `run_qcc_tool`: 企業スクリーニングを実行（条件が揃った場合のみ）。
    3. `response_to_user`: ユーザーに追加質問をする、または回答する。

    **【出力フォーマット】**
    必ず以下のJSON形式のみを出力してください。Markdownは不要です。

    ```json
    {
        "thought": "ユーザーは自動車ガラスメーカー。顧客は自動車組立工場(OEM)や修理工場だ。地域が指定されていないため、まずは一般的な顧客層をナレッジベースで確認しよう。",
        "action": "consult_knowledge_base",
        "params": {
            "query": "汽车玻璃 主要客户 供应链"  <-- 注意：検索効率のため、ここは必ず中国語で入力すること。
        }
    }
    ```
    
    または
    
    ```json
    {
        "thought": "地域は上海と指定された。ターゲットは自動車OEM工場。条件は揃った。",
        "action": "run_qcc_tool",
        "params": {
            "guidance_text": "検索対象：自動車組立工場。\nキーワード（中国語で記入）：汽车制造、汽车零部件加工。\n地域：上海。\n除外：ガラス製造（競合のため）。"
        }
    }
    ```

    または
    
    ```json
    {
        "thought": "ナレッジベースでターゲットは判明したが、地域が不明だ。ユーザーに聞く必要がある。",
        "action": "response_to_user",
        "params": {
            "text": "ターゲットとして自動車組立工場が考えられます。スクリーニングを行いたい「地域」（例：関東、中国・広東省など）を教えていただけますか？"
        }
    }
    ```
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
            # フロントエンドへの表示用
            yield f"data: [TEXT_RESPONSE]{resp_text.replace('\n', '\\n')}\n\n"
            yield "data: ---END_OF_STREAM---\n\n"
            return  # ユーザー入力待ちへ

        # CASE 2: ナレッジベース検索
        elif action == "consult_knowledge_base":
            query = params.get("query", "")
            yield f"data: [STATUS_MSG]ナレッジベース検索中: {query}...\n\n"

            rag_result = await asyncio.to_thread(query_knowledge_base, rag_index, query)

            # 結果を履歴に追加（Tool Role）
            tool_msg = f"【Tool: Knowledge Base Result】\n{rag_result}"
            history.append({"role": "tool", "content": tool_msg})

            # 情報を保持したまま次のループへ（continue）
            continue

        # CASE 3: スクリーニングツール実行
        elif action == "run_qcc_tool":
            guidance_text = params.get("guidance_text", "")

            # 意図を記録
            # 注: historyへの追加は上記でLLMレスポンス全体を追加済みだが、明確化のため補足情報を入れることも可能
            # ここではLLMの判断結果として処理を進める

            # --- Playwright 実行準備 ---
            playwright_test.LLM_GUIDANCE_TEXT = guidance_text
            logger_instance = PlaywrightLogger(log_queue)

            # 実行タスク
            def _sync_run():
                if sys.platform == "win32":
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                try:
                    asyncio.run(
                        playwright_test.test_qcc_llm_interaction_with_playwright(logger_instance, guidance_text))
                except Exception as e:
                    error_msg = traceback.format_exc()
                    logger_instance.log_to_frontend(f"❌ Error: {error_msg}")
                    logger.error(f"Playwright Error: {error_msg}")

            future = asyncio.to_thread(_sync_run)
            task = asyncio.create_task(future)

            final_report_content = ""

            # ストリーミングループ
            while True:
                queue_task = asyncio.create_task(log_queue.get())
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
            while not log_queue.empty():
                try:
                    msg = log_queue.get_nowait()
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

        else:
            # 未知のアクション
            err_msg = f"Unknown action: {action}"
            logger.error(err_msg)
            history.append({"role": "tool", "content": f"Error: {err_msg}"})
            continue

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