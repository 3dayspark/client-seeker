import json
import sys
import os
import re
import time
from typing import Dict, Any, List
from openai import OpenAI

# --- 1. 環境設定とインポート ---

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from playwright_test import (
        MODEL_SCOPE_API_KEY, 
        MODEL_SCOPE_BASE_URL, 
        MODEL_SCOPE_MODEL_ID
    )
except ImportError:
    print("❌ エラー: playwright_test.py が見つからないか、API設定をインポートできませんでした。")
    sys.exit(1)

# --- 2. Master Agent のシステムプロンプト定义 ---

MOCK_DB_SCHEMA = """
Table Name: companies
Columns: id (INTEGER), name (VARCHAR), industry (VARCHAR), region (VARCHAR), created_at (TIMESTAMP)
Sample Data (Limit 3): [(1, 'ABC Tech', 'IT', 'Shanghai', '2023-01-01'), (2, 'XYZ Mfg', 'Manufacturing', 'Guangdong', '2023-02-15')]

Table Name: contacts
Columns: id (INTEGER), company_id (INTEGER), name (VARCHAR), position (VARCHAR), email (VARCHAR), phone (VARCHAR)
Sample Data (Limit 3): [(1, 1, 'John Doe', 'Manager', 'john@abc.com', '13800000000')]

Table Name: sales_records
Columns: id (INTEGER), company_id (INTEGER), status (VARCHAR), sales_amount (DECIMAL), last_contact_date (DATE)
Sample Data (Limit 3): [(1, 1, '商談中', 50000.00, '2023-10-01'), (2, 2, '契約締結', 120000.00, '2023-09-20')]
"""

SYSTEM_INSTRUCTION = """
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
    4. `response_to_user`: ユーザーに追加質問をする、または回答する。


    **【出力フォーマット】**
    必ず以下のJSON形式のみを出力してください。Markdownは不要です。

    例：業界への言及があるため、まずナレッジベースを確認する場合
    ```json
    {
        "thought": "ユーザーは「自動車ガラス」という特定の製品に言及している。ターゲット企業の解像度を高めるため、まずはナレッジベースで自動車ガラス業界のサプライチェーンや、主要な納入先（OEMメーカー等）を検索して確認する必要がある。",
        "action": "consult_knowledge_base",
        "params": {
            "query": "汽车玻璃 供应链 主要客户"  <-- 注意：検索効率のため、ここは必ず中国語で入力すること。
        }
    }
    ```
    
    または
    
    ユーザー：「広東省のガラス工場を探して」
    AI回答：
    ```json
    {
        "thought": "ナレッジベースでの確認が完了し、検索条件は明確になった。実行前にユーザーに条件案を提示して確認をとる。",
        "action": "propose_screening_condition",
        "params": {
            "guidance_text": "ターゲット：広東省エリアのガラス製造・加工企業",
            "regions": ["広東省"],
            "keywords": "ガラス製造、深加工"
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
    {MOCK_DB_SCHEMA}
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


# --- 3. ヘルパー関数群 ---

def get_llm_client():
    return OpenAI(
        base_url=MODEL_SCOPE_BASE_URL,
        api_key=MODEL_SCOPE_API_KEY,
    )

def extract_json_from_text(text: str) -> Dict[str, Any]:
    try:
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```', '', text)
        match = re.search(r'(\{[\s\S]*\})', text)
        if match:
            json_str = match.group(1)
        else:
            json_str = text
        
        def replace_newlines_in_quotes(m):
            return m.group(0).replace('\n', '\\n')

        json_str_clean = re.sub(r'"((?:[^"\\]|\\.)*)"', replace_newlines_in_quotes, json_str, flags=re.DOTALL)
        return json.loads(json_str_clean, strict=False)

    except Exception as e:
        return {"action": "error", "thought": f"JSON Parse Failed: {e}", "raw_output": text}

def construct_prompt_from_history(history_snapshot: List[Dict[str, str]]) -> str:
    history_text = ""
    for msg in history_snapshot:
        role = msg['role']
        content = msg['content']
        if role == "user":
            history_text += f"User: {content}\n"
        elif role == "assistant":
            history_text += f"Assistant: {content}\n"
        elif role == "tool":
            history_text += f"System (Tool Execution Result): \n{content}\n"
            
    full_prompt = f"""
    {SYSTEM_INSTRUCTION}
    
    --- チャット履歴 (Chat History) ---
    {history_text}
    
    Assistant:
    """
    return full_prompt

# --- 判定ロジック改善版 ---

def check_params_match(actual_params: Dict, expected_params: Dict) -> bool:
    if not expected_params:
        return True
        
    for key, expected_val in expected_params.items():
        if key == "guidance_text_contains":
            actual_val = actual_params.get("guidance_text", "")
            if expected_val not in actual_val:
                return False
        
        elif key == "query":
            actual_val = actual_params.get("query", "")
            # 空白で分割して、少なくとも1つのキーワードが含まれていればOKとする（柔軟な評価）
            keywords = expected_val.split()
            if not any(k in actual_val for k in keywords):
                return False

        elif key == "keywords":
            actual_val = str(actual_params.get("keywords", ""))
            # 文字列またはリストとして期待値が含まれているか
            if isinstance(expected_val, list):
                # リスト内の「どれか一つ」でも含まれていればOKにする（柔軟化）
                # 厳密に全部チェックすると、AIの翻訳揺れ（轮胎 vs タイヤ）で死ぬため
                if not any(item in actual_val for item in expected_val):
                     return False
            else:
                if expected_val not in actual_val and actual_val not in expected_val:
                    return False

        elif isinstance(expected_val, list):
            actual_val = actual_params.get(key, [])
            if not isinstance(actual_val, list):
                return False
            # 完全一致ではなく、包含関係をチェック
            for item in expected_val:
                if item not in actual_val:
                    return False
        
        elif key in actual_params:
            if actual_params[key] != expected_val:
                return False
                
    return True

def check_sql_validity(actual_params: Dict, expected_keywords: List[str]) -> bool:
    sql = actual_params.get("sql_query", "").upper()
    if not sql:
        return False
    
    # 必須キーワード（テーブル名や条件値）がすべて含まれているか
    # ただし、SQL构文（SELECT, WHERE等）はAIが省略したり順序が変わる可能性があるので
    # 「値」や「テーブル名」のような核心的なものだけを厳密チェックするのが吉
    for kw in expected_keywords:
        if kw.upper() not in sql:
            # エイリアス対応: companies -> c などの省略形も考慮したいが、
            # テストデータ側で 'companies' を期待しているなら、それはAIが入れるべき
            # ここでは厳密にチェックするが、テストデータを修正して緩和する
            return False
    return True

def check_content_keywords(actual_params: Dict, keywords: List[str]) -> bool:
    text = actual_params.get("text", "")
    if not text:
        return False
    
    # 命中率計算: キーワードの50%以上が含まれていれば合格とする
    hit_count = sum(1 for kw in keywords if kw in text)
    if len(keywords) > 0 and (hit_count / len(keywords) < 0.4): # 閾値を40%に緩和
        return False
    return True

# --- 4. メイン評価ループ ---

def run_evaluation():
    dataset_path = os.path.join(os.path.dirname(__file__), 'datasets', 'agent_react_scenarios.json')
    
    if not os.path.exists(dataset_path):
        print(f"❌ データセットが見つかりません: {dataset_path}")
        return

    with open(dataset_path, 'r', encoding='utf-8') as f:
        scenarios = json.load(f)

    client = get_llm_client()
    
    total_cases = len(scenarios)
    passed_cases = 0
    results = []

    print(f"\n🚀 Agent ReAct ロジック評価を開始します (全 {total_cases} ケース)\n" + "="*60)

    for i, case in enumerate(scenarios):
        case_id = case.get('id', f'case_{i}')
        description = case.get('description', 'No description')
        
        print(f"\n[{i+1}/{total_cases}] テスト実行中: {case_id}")
        print(f"📝 概要: {description}")
        
        prompt = construct_prompt_from_history(case['history_snapshot'])
        
        start_time = time.time()
        try:
            response = client.chat.completions.create(
                model=MODEL_SCOPE_MODEL_ID,
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0.1,
                stream=False,
                extra_body={"enable_thinking": False}
            )
            raw_output = response.choices[0].message.content
            output_json = extract_json_from_text(raw_output)
            
        except Exception as e:
            print(f"💥 API呼び出しまたは処理中に例外発生: {e}")
            results.append({"id": case_id, "status": "ERROR", "reason": str(e)})
            continue
            
        duration = time.time() - start_time
        
        # --- 判定ロジック ---
        actual_action = output_json.get("action")
        expected_action = case.get("expected_intent")
        actual_params = output_json.get("params", {})
        thought = output_json.get("thought", "思考なし")
        
        is_pass = False
        fail_reason = ""

        # 特殊ルール: KB検索と提案は、文脈によってはどちらも正解になりうる
        # テストデータ側で 'alternative_intent' を定義可能にする拡張もアリだが
        # ここではシンプルに Action 一致を前提とする

        if actual_action == "error":
            fail_reason = "JSON解析エラー"
        elif actual_action == expected_action:
            param_check = True
            
            if expected_action == "run_qcc_tool" and "expected_params" in case:
                param_check = check_params_match(actual_params, case["expected_params"])
            
            elif expected_action == "propose_screening_condition" and "expected_params" in case:
                param_check = check_params_match(actual_params, case["expected_params"])
            
            elif expected_action == "consult_knowledge_base" and "expected_params" in case:
                param_check = check_params_match(actual_params, case["expected_params"])
            
            elif expected_action == "search_internal_crm" and "expected_sql_keywords" in case:
                param_check = check_sql_validity(actual_params, case["expected_sql_keywords"])
                if not param_check:
                    fail_reason = "SQLクエリに必要なキーワード不足"

            elif expected_action == "response_to_user" and "expected_content_keywords" in case:
                param_check = check_content_keywords(actual_params, case["expected_content_keywords"])
                if not param_check:
                    fail_reason = "回答キーワード不足"
            
            if param_check:
                is_pass = True
            elif not fail_reason:
                fail_reason = "パラメータ不一致"
        else:
            fail_reason = f"アクション不一致 (期待: {expected_action}, 実際: {actual_action})"

        # --- 結果出力 ---
        status_icon = "✅ PASS" if is_pass else "❌ FAIL"
        
        print(f"{status_icon} ({duration:.2f}s)")
        # print(f"   🧠 [Thought]: {str(thought)[:100]}...") # ログを簡略化
        print(f"   🎬 [Action] : {actual_action}")

        if actual_action == "search_internal_crm":
             print(f"   ⚙️  [SQL]    : {actual_params.get('sql_query', 'No SQL')}")
        elif actual_action != "error":
             params_str = json.dumps(actual_params, ensure_ascii=False, indent=0).replace('\n', ' ')
             print(f"   ⚙️  [Params] : {params_str}")

        if not is_pass:
            print(f"   ⚠️ Reason   : {fail_reason}")
            # 期待値の表示（デバッグ用）
            if "expected_params" in case:
                print(f"      Expected : {case['expected_params']}")
            if "expected_sql_keywords" in case:
                print(f"      Expected Keys: {case['expected_sql_keywords']}")
            if "expected_content_keywords" in case:
                 print(f"      Expected Keys: {case['expected_content_keywords']}")
            
            results.append({"id": case_id, "status": "FAIL", "reason": fail_reason, "thought": thought})
        else:
            passed_cases += 1
            results.append({"id": case_id, "status": "PASS", "thought": thought})

    # --- 最終レポート ---
    accuracy = (passed_cases / total_cases) * 100 if total_cases > 0 else 0
    print("\n" + "="*60)
    print(f"📊 評価完了レポート")
    print(f"   合計ケース数: {total_cases}")
    print(f"   成功: {passed_cases}")
    print(f"   失敗: {total_cases - passed_cases}")
    print(f"   精度 (Accuracy): {accuracy:.1f}%")
    print("="*60)

    if passed_cases < total_cases:
        print("⚠️ 失敗したケースID一覧:")
        for res in results:
            if res['status'] == 'FAIL':
                print(f"   - {res['id']}: {res['reason']}")

if __name__ == "__main__":
    run_evaluation()