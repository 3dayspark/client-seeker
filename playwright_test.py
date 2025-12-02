import json
import logging
import time
import re
import os
import sys
import base64
import asyncio
import itertools
import requests
from typing import List, Dict, Any, Optional, Set
from playwright.async_api import async_playwright, Page, Browser, Locator, ElementHandle
from openai import OpenAI

# 標準ロガーの設定（スクリプト初期化時のエラー出力用）
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_api_keys(json_filename='api_keys.json'):
    """
    APIキー設定ファイルを読み込みます。
    """
    try:
        # スクリプトの絶対パスを取得
        current_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(current_dir, json_filename)
        
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        # Gemini Keys (リスト) の抽出
        gemini_keys = data.get("gemini", [])
        
        # ModelScope Key の抽出 (リストの0番目、存在しない場合は空文字)
        ms_list = data.get("modelscope", [])
        ms_key = ms_list[0] if ms_list else ""
        
        return gemini_keys, ms_key

    except Exception as e:
        logger.warning(f"⚠️ keys.json の読み込みに失敗しました: {e}。デフォルトの空値を使用するか、ファイルパスを手動で確認してください。")
        return [], ""

_loaded_gemini_keys, _loaded_ms_key = load_api_keys()

# --- ModelScope 設定 ---
MODEL_SCOPE_API_KEY = _loaded_ms_key 
MODEL_SCOPE_BASE_URL = 'https://api-inference.modelscope.cn/v1'
MODEL_SCOPE_MODEL_ID = 'Qwen/Qwen3-32B' 

# --- Gemini API 設定 ---
GEMINI_API_KEYS = _loaded_gemini_keys
GEMINI_API_URL = "https://geminiapi.asynchronousblocking.asia/v1beta/models/gemini-2.5-flash-lite:generateContent"

# --- LLM 切り替え設定 ---
USE_GEMINI_AS_LLM = False # True: Geminiを使用, False: ModelScopeを使用

MAX_RETRIES = 3
INITIAL_DELAY_SECONDS = 2
BATCH_SIZE_FOR_LLM_SELECTION = 300 # LLMに一度に提示する選択肢の数

modelscope_client = None
gemini_api_key_pool = None

# --- Gemini API ヘッダー ---
gemini_base_headers = {
    "Content-Type": "application/json",
}

# --- LLM 意思決定ガイダンス ---
LLM_GUIDANCE_TEXT = "広東省の経営が良好で、規模の大きい海鮮市場を探したい"

EXECUTION_SUMMARY = {
    "keywords": [],
    "regions": [],
    "checkboxes": {}, # {"大分類": ["選択肢1", "選択肢2"]}
    "dropdowns": {},  # {"大分類": {"メニュー名": ["選択肢"]}}
    "industry_tree": [],
    "reasons": {}
}

# --- キャッシュ設定 ---
ENABLE_CACHE = True
CACHE_DIR = "local_page_data_cache"
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

INDUSTRY_CACHE_DIR = "industry_nodes"
if not os.path.exists(INDUSTRY_CACHE_DIR):
    os.makedirs(INDUSTRY_CACHE_DIR)


async def _capture_and_send_screenshot(Logger, page: Page, caption: str = ""):
    """
    スクリーンショットを撮影し、フロントエンドに送信します。
    """
    try:
        # 可視領域をキャプチャ (full_page=False はプロセス表示用)
        screenshot_bytes = await page.screenshot(full_page=False)
        # Base64 に変換
        b64_str = base64.b64encode(screenshot_bytes).decode('utf-8')
        # 特殊フォーマットのログを送信: [SCREENSHOT]base64文字列
        Logger.log_to_frontend(f"[SCREENSHOT]{b64_str}")
        if caption:
             Logger.log_to_frontend(f"📸 画面更新: {caption}")
    except Exception as e:
        Logger.log_to_frontend(f"スクリーンショットの撮影に失敗しました: {e}")

def _generate_final_report():
    """
    実行結果のサマリーレポートを生成します。
    """
    lines = []
    reasons = EXECUTION_SUMMARY.get("reasons", {})
    
    # 1. 検索キーワード
    if EXECUTION_SUMMARY.get("keywords"):
        kws = "、".join([f'“{k}”' for k in EXECUTION_SUMMARY["keywords"]])
        current_line = f"検索キーワード：{kws}"
        if "keywords" in reasons and reasons["keywords"]:
            current_line += f"||REASON||{reasons['keywords']}"
        lines.append(current_line)
    
    # 2. 省・地域
    if EXECUTION_SUMMARY.get("regions"):
        regs = "、".join([f'“{r}”' for r in EXECUTION_SUMMARY["regions"]])
        current_line = f"省・地域：{regs}"
        if "regions" in reasons and reasons["regions"]:
            current_line += f"||REASON||{reasons['regions']}"
        lines.append(current_line)
        
    # 3. Checkbox (大分類1タイトル)
    if EXECUTION_SUMMARY.get("checkboxes"):
        for category, options in EXECUTION_SUMMARY["checkboxes"].items():
            valid_opts = [o for o in options if o and "取得できません" not in o]
            if valid_opts:
                opts_str = "、".join([f'“{o}”' for o in valid_opts])
                lines.append(f"{category}：チェック {opts_str}")

        if "checkboxes" in reasons and reasons["checkboxes"]:
            lines.append(f"||REASON||{reasons['checkboxes']}")

    # 4. Dropdowns (大分類2/3タイトル)
    if EXECUTION_SUMMARY.get("dropdowns"):
        dropdown_data = EXECUTION_SUMMARY["dropdowns"]
        clean_dropdowns = {} 
        dirty_keys = ["normal_dropdown_selections", "radio_dropdown_selections"]
        
        # Normal タイプの処理
        for item in dropdown_data.get("normal_dropdown_selections", []):
            if item.get('selection'):
                sel = item.get('selector', '')
                match = re.search(r'has-text\("([^"]+)"\)', sel)
                menu_name = match.group(1) if match else "その他メニュー"
                if "詳細オプション" not in clean_dropdowns: clean_dropdowns["詳細オプション"] = {}
                if menu_name not in clean_dropdowns["詳細オプション"]: clean_dropdowns["詳細オプション"][menu_name] = []
                clean_dropdowns["詳細オプション"][menu_name].append(item['selection'])

        # Radio タイプの処理
        for item in dropdown_data.get("radio_dropdown_selections", []):
            selections = item.get('selections', [])
            if selections:
                sel = item.get('selector', '')
                match = re.search(r'has-text\("([^"]+)"\)', sel)
                menu_name = match.group(1) if match else "その他メニュー"
                vals = [s['choice'] for s in selections if 'choice' in s]
                if vals:
                    if "詳細オプション" not in clean_dropdowns: clean_dropdowns["詳細オプション"] = {}
                    if menu_name not in clean_dropdowns["詳細オプション"]: clean_dropdowns["詳細オプション"][menu_name] = []
                    clean_dropdowns["詳細オプション"][menu_name].extend(vals)

        # クレンジング済み dict 構造の処理
        for cat, content in dropdown_data.items():
            if cat in dirty_keys: continue 
            if isinstance(content, dict):
                if cat not in clean_dropdowns: clean_dropdowns[cat] = {}
                clean_dropdowns[cat].update(content)

        # Dropdown テキストの出力
        if clean_dropdowns:
            for category, menus in clean_dropdowns.items():
                if not menus: continue
                lines.append(f"{category}：")
                idx = 1
                for menu_name, options in menus.items():
                    if not options: continue
                    if isinstance(options, str): options = [options]
                    opts_str = "、".join([f'“{o}”' for o in options])
                    lines.append(f"{idx}、{menu_name}：選択 {opts_str}")
                    idx += 1

            if "dropdowns" in reasons and reasons["dropdowns"]:
                lines.append(f"||REASON||{reasons['dropdowns']}")

    # 5. 業界選択
    industry_nodes = EXECUTION_SUMMARY.get("industry_tree", [])
    
    if industry_nodes:
        nodes_str = "、".join([f'“{n}”' for n in industry_nodes])
        lines.append(f"所属業界：{nodes_str}")
    
    industry_reason_parts = []
    
    # A. 大分類スクリーニング理由
    if "industry_top_level" in reasons and reasons["industry_top_level"]:
        industry_reason_parts.append(f"【大分類特定】{reasons['industry_top_level']}")
    
    # B. 詳細スクリーニング理由
    for key, val in reasons.items():
        if key.startswith("industry_") and key != "industry_top_level":
            cat_name = key.replace("industry_", "")
            industry_reason_parts.append(f"【{cat_name}】{val}")
            
    # C. 旧ロジック互換
    if "industry" in reasons and reasons["industry"]:
        industry_reason_parts.append(f"{reasons['industry']}")

    if industry_reason_parts:
        combined_reason = "；".join(industry_reason_parts)
        if industry_nodes:
             lines[-1] += f"||REASON||{combined_reason}"
        else:
             lines.append(f"業界特定ロジック：||REASON||{combined_reason}")

    return "||NEWLINE||".join(lines)


def _load_from_cache(logger, filename: str) -> Optional[Any]:
    """
    ローカルキャッシュからのデータ読み込みを試行します。
    """
    if not ENABLE_CACHE:
        return None
    filepath = os.path.join(CACHE_DIR, filename)
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.log_to_frontend(f"  - 📂 [キャッシュ] '{filename}' からデータをロードしました。収集処理をスキップします。")
            return data
        except Exception as e:
            logger.log_to_frontend(f"  - ⚠️ [キャッシュ] ファイル '{filename}' の読み込みに失敗しました: {e}")
    return None

def _save_to_cache(logger, filename: str, data: Any):
    """
    データをローカルキャッシュに保存します。
    """
    if not ENABLE_CACHE:
        return
    filepath = os.path.join(CACHE_DIR, filename)
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        logger.log_to_frontend(f"  - 💾 [キャッシュ] データは '{filename}' に保存されました。")
    except Exception as e:
        logger.log_to_frontend(f"  - ⚠️ [キャッシュ] ファイル '{filename}' の保存に失敗しました: {e}")


async def _call_modelscope_api(Logger, prompt: str) -> str:
    """
    ModelScope API を呼び出し、指数バックオフリトライを行います。
    """
    global modelscope_client

    if modelscope_client is None:
        try:
            if not MODEL_SCOPE_API_KEY or "YOUR_API_KEY" in MODEL_SCOPE_API_KEY:
                raise ValueError("ModelScope API キーが設定されていません。")
            Logger.log_to_frontend(f" - ModelScope API クライアントの設定に成功しました。使用モデル：'{MODEL_SCOPE_MODEL_ID}'")
            modelscope_client = OpenAI(
                base_url=MODEL_SCOPE_BASE_URL,
                api_key=MODEL_SCOPE_API_KEY,
            )
        except Exception as e:
            Logger.log_to_frontend(f"❌ ModelScope サービスの構成に失敗しました: {e}")
            return ""
            
    current_delay = INITIAL_DELAY_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            Logger.log_to_frontend(f" - ModelScope API を呼び出し中 (試行 {attempt + 1}/{MAX_RETRIES})...")
            response = modelscope_client.chat.completions.create(
                model=MODEL_SCOPE_MODEL_ID,
                messages=[{
                    'role': 'user',
                    'content': [{'type': 'text', 'text': prompt}],
                }],
                stream=False,
                extra_body={"enable_thinking": False}
            )

            full_response_content = ""
            if isinstance(response, object) and hasattr(response, 'choices'):
                for choice in response.choices:
                    if hasattr(choice.message, 'content') and choice.message.content:
                        full_response_content += choice.message.content
            return full_response_content.strip()

        except Exception as e:
            Logger.log_to_frontend(f" - ❌ ModelScope API 呼び出し失敗: {e}")
            if attempt < MAX_RETRIES - 1:
                Logger.log_to_frontend(f" - {current_delay} 秒後にリトライします...")
                await asyncio.sleep(current_delay)
                current_delay *= 2
            else:
                Logger.log_to_frontend(f"❌ 最大リトライ回数に達しました。ModelScope API の呼び出しを中止します。")
                return ""
    return ""


async def _call_gemini_api(Logger, prompt: str) -> str:
    """
    Gemini API を呼び出します（APIキーローテーション対応）。
    """
    global gemini_api_key_pool
    
    if gemini_api_key_pool is None:
        try:
            if not all(GEMINI_API_KEYS) or any("YOUR_GEMINI_API_KEY" in key for key in GEMINI_API_KEYS):
                raise ValueError("有効な Gemini API キーが設定されていません。")
            Logger.log_to_frontend(" - Gemini API クライアントの設定に成功しました。")
            gemini_api_key_pool = itertools.cycle(GEMINI_API_KEYS)
        except Exception as e:
            Logger.log_to_frontend(f"❌ Gemini サービスの構成に失敗しました: {e}")
            return ""

    current_delay = INITIAL_DELAY_SECONDS
    tried_keys_in_cycle = set()
    
    for attempt in range(MAX_RETRIES):
        current_key = next(gemini_api_key_pool)
        
        if current_key in tried_keys_in_cycle:
            Logger.log_to_frontend(f" - すべての Gemini キーが試行済み、またはレート制限に達しました。{current_delay} 秒待機してから再試行します...")
            await asyncio.sleep(current_delay)
            current_delay *= 2
            tried_keys_in_cycle.clear()
            continue
        
        tried_keys_in_cycle.add(current_key)
        
        headers = gemini_base_headers.copy()
        headers["X-goog-api-key"] = current_key

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ]
        }
        
        response = None
        try:
            Logger.log_to_frontend(f" - Gemini キー '{current_key[-6:]}...' を使用してAPIを呼び出し中 (試行 {attempt + 1}/{MAX_RETRIES})...")
            response = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=120)
            
            if response.status_code == 429:
                Logger.log_to_frontend(f" - ⚠️ Gemini キー レート制限 ({current_key[-6:]}...)、次のキーに切り替えます...")
                continue
            
            response.raise_for_status()

            response_data = response.json()
            if 'candidates' in response_data and response_data['candidates']:
                first_candidate = response_data['candidates'][0]
                if 'content' in first_candidate and 'parts' in first_candidate['content']:
                    first_part = first_candidate['content']['parts'][0]
                    if 'text' in first_part:
                        return first_part['text'].strip()
            
            Logger.log_to_frontend(f" - ❌ Gemini API レスポンス形式異常、または内容がありません。生レスポンス: {response.text}")
            return ""

        except requests.exceptions.RequestException as e:
            if response is not None:
                if response.status_code == 429:
                    Logger.log_to_frontend(f" - ⚠️ Gemini キー レート制限 ({current_key[-6:]}...)、次のキーに切り替えます...")
                    continue
                elif response.status_code == 400:
                    Logger.log_to_frontend(f" - ❌ Gemini API 400 Bad Request ({current_key[-6:]}...)。リクエスト内容を確認してください。エラー詳細: {e}")
                    continue
                elif response.status_code == 503:
                    Logger.log_to_frontend(f" - ❌ Gemini サービス一時的に利用不可 (503)。{current_delay} 秒後にリトライします...")
                    await asyncio.sleep(current_delay)
                    current_delay *= 2
                    tried_keys_in_cycle.clear()
                    continue
            
            Logger.log_to_frontend(f" - ❌ Gemini API 呼び出し失敗: {e}")
            return ""
        except Exception as e:
            Logger.log_to_frontend(f" - ❌ Gemini API レスポンス処理失敗または未知のエラー: {e}")
            return ""

    Logger.log_to_frontend(f"❌ 最大リトライ回数に達しました。Gemini API の呼び出しを中止します。")
    return ""


async def _call_llm_for_decision_json(Logger, prompt: str) -> Optional[Any]:
    """
    LLM を呼び出し、返された JSON の解析を試みます。
    """
    if USE_GEMINI_AS_LLM:
        Logger.log_to_frontend(" - Gemini API を LLM として使用します。")
        response_text = await _call_gemini_api(Logger, prompt)
    else:
        Logger.log_to_frontend(" - ModelScope API を LLM として使用します。")
        response_text = await _call_modelscope_api(Logger, prompt)

    if not response_text:
        return None

    try:
        stripped_response = response_text.strip()
        json_str = stripped_response

        # Markdown コードブロックの除去処理
        if stripped_response.startswith('```') and stripped_response.endswith('```'):
            start_code_block_index = stripped_response.find('\n')
            if start_code_block_index != -1:
                json_str = stripped_response[start_code_block_index + 1 : -len('```')].strip()
            else:
                json_str = stripped_response

        return json.loads(json_str)

    except json.JSONDecodeError as e:
        Logger.log_to_frontend(f"❌ LLMの出力結果をJSONとして解析できませんでした: {e}")
        Logger.log_to_frontend(f"LLM 生出力: \n{response_text}")
        return None
    except Exception as e:
        Logger.log_to_frontend(f"LLM レスポンス処理中に未知のエラーが発生しました: {e}")
        return None


def _clean_html_text(text_content: str) -> str:
    """
    テキストから空の <em></em> タグを除去します。
    例: "<em></em>农<em></em>、<em></em>林..." -> "农、林..."
    """
    return re.sub(r'<em><\/em>', '', text_content)


async def _collect_targeted_input_element_data(Logger, page: Page, target_placeholder: str = "输入关键词", target_class: str = "qccd-input") -> List[Dict[str, Any]]:
    """
    指定された placeholder と class を持つ input 要素の情報を収集します。
    範囲は '.advance-filters-container' 内に限定します。
    """
    input_details = []

    advance_filters_container = page.locator('.advance-filters-container')
    if await advance_filters_container.count() == 0 or not await advance_filters_container.is_visible():
        Logger.log_to_frontend(" - 警告: 'advance-filters-container' が検出できないか不可視です。input要素情報の収集を中止します。")
        return []

    input_element = None
    final_selector = ""
    is_unique_and_visible_selector = False

    desired_specific_selector = f"input.{target_class}[placeholder='{target_placeholder}']"
    
    targeted_locator_in_container = advance_filters_container.locator(desired_specific_selector)
    
    if await targeted_locator_in_container.count() == 1:
        input_element = targeted_locator_in_container.first 
        final_selector = desired_specific_selector
        is_unique_and_visible_selector = True
    elif await targeted_locator_in_container.count() > 1: 
        Logger.log_to_frontend(f" - 警告: '.advance-filters-container' 内に複数の可視 input 要素が見つかりました。最初の要素を使用します。")
        input_element = targeted_locator_in_container.first 
        final_selector = desired_specific_selector
        is_unique_and_visible_selector = False
    else:
        Logger.log_to_frontend(f" - エラー: 指定された input 要素が見つかりませんでした。")
        return []

    if input_element is None:
        return []

    try:
        input_name = await input_element.get_attribute('name') or ""
        input_id = await input_element.get_attribute('id') or ""
        input_placeholder = await input_element.get_attribute('placeholder') or ""
        input_aria_label = await input_element.get_attribute('aria-label') or ""
        input_title = await input_element.get_attribute('title') or ""
        input_type = await input_element.get_attribute('type') or "text"
        current_value = await input_element.get_attribute('value') or ""

        if input_id:
            candidate_selector_id = f"#{input_id}"
            try:
                locator_test_id = page.locator(candidate_selector_id)
                if await locator_test_id.count() == 1 and await locator_test_id.is_visible():
                    final_selector = candidate_selector_id
                    is_unique_and_visible_selector = True
            except Exception:
                pass
        
        if not is_unique_and_visible_selector and input_name:
            candidate_selector_name = f"input[name='{input_name}']"
            try:
                locator_test_name = page.locator(candidate_selector_name)
                if await locator_test_name.count() == 1 and await locator_test_name.is_visible():
                    final_selector = candidate_selector_name
                    is_unique_and_visible_selector = True
            except Exception:
                pass
        
        local_html_snippet = ""
        try:
            parent_locator_for_snippet = input_element.locator('xpath=./ancestor-or-self::div[1]|./ancestor-or-self::span[1]|./ancestor-or-self::label[1]')
            if await parent_locator_for_snippet.count() > 0:
                local_html_snippet = await parent_locator_for_snippet.first.evaluate("el => el.outerHTML") 
                local_html_snippet = local_html_snippet[:500]
        except Exception as html_e:
            Logger.log_to_frontend(f" - HTMLスニペット収集中にエラーが発生しました: {html_e}")
            local_html_snippet = ""

        input_details.append({
            "index": 1,
            "selector": final_selector,
            "input_type": input_type,
            "placeholder": input_placeholder,
            "name_attribute": input_name,
            "id_attribute": input_id,
            "aria_label_attribute": input_aria_label,
            "title_attribute": input_title,
            "current_value": current_value,
            "local_html_snippet": local_html_snippet,
            "is_unique_and_visible_selector": is_unique_and_visible_selector
        })

    except Exception as e:
        Logger.log_to_frontend(f" - input 要素情報収集中にエラーが発生しました: {e}")
        pass

    return input_details


async def _handle_region_selection(Logger, page: Page):
    """
    LLM に特定の地域を検索するかどうかを決定させ、自動的に「省・地域」フィルターを操作します。
    """
    Logger.log_to_frontend("\n🌍 **フェーズ 1.5: LLM による地域選択の判定と実行**")
    
    # 1. LLM への問い合わせ
    region_prompt = f"""
    你是一个专业的企业搜索助手。请根据目标企业画像，判断是否需要限定具体的中国行政区域（省份、直辖市）。
    
    **目标企业画像:** "{LLM_GUIDANCE_TEXT}"
    
    请返回如下 JSON 格式：
    {{
        "reason": "（这个字段请用日语填写）判断需要/不需要限定地区的理由（例如：用户明确提到了广东，或者用户寻找的是全国性业务）",
        "regions":（这个字段请用中文填写） [ "上海市", "广州市" ] 
    }}
    如果不需要限定，regions 返回空数组 []。
    """

    Logger.log_to_frontend("  - 地域指定の必要性を LLM に問い合わせ中...")
    result_json = await _call_llm_for_decision_json(Logger, region_prompt)
    
    target_regions = []
    if result_json and isinstance(result_json, dict):
        target_regions = result_json.get("regions", [])
        EXECUTION_SUMMARY["reasons"]["regions"] = result_json.get("reason", "理由なし")

    if target_regions:
        EXECUTION_SUMMARY["regions"] = target_regions

    if not target_regions or not isinstance(target_regions, list) or len(target_regions) == 0:
        Logger.log_to_frontend("  - 地域指定は不要と判断されました。スキップします。")
        return

    Logger.log_to_frontend(f"  - LLMによる指定地域: {target_regions}")

    # 2. 検索ボックスの特定と操作
    try:
        title_locator = page.locator("div.into-one-title.m-r-sm", has_text="省份地区")
        item_container = page.locator("div.into-one-item").filter(has=title_locator)
        cascader_div = item_container.locator("div.adv-selelct-cascader")
        search_input = cascader_div.locator("input.search-input")

        if await search_input.count() == 0:
            Logger.log_to_frontend("  - エラー: '省份地区' の検索入力ボックスが見つかりません。")
            return

        # 3. 各地域キーワードの処理
        for region in target_regions:
            region = region.strip()
            if not region: continue
            
            Logger.log_to_frontend(f"  - 地域: [{region}] を処理中...")
            
            try:
                await search_input.click()
                await search_input.fill("")
                await search_input.type(region, delay=50)
                
                await page.wait_for_timeout(800)

                target_li_selector = f"div.drop-container.qccd-dropdown-content li[title='{region}']"
                target_li = page.locator(target_li_selector)
                
                visible_target_li = target_li.filter(has=page.locator("visible=true")).first
                
                if await visible_target_li.count() > 0 and await visible_target_li.is_visible():
                    checkbox = visible_target_li.locator("input.qccd-checkbox-input")
                    
                    if not await checkbox.is_checked():
                        await checkbox.check(force=True)
                        Logger.log_to_frontend(f"    - ✅ 選択成功: {region}")
                    else:
                        Logger.log_to_frontend(f"    - ヒント: {region} は既に選択済みです。")
                else:
                    Logger.log_to_frontend(f"    - ⚠️ 一致する可視オプションが見つかりません: {region}")

            except Exception as e:
                Logger.log_to_frontend(f"    - ❌ 地域 [{region}] 処理中にエラーが発生しました: {e}")
                continue
        
        await _capture_and_send_screenshot(Logger, page, "地域選択完了")
        
        try:
            await title_locator.click()
        except:
            pass

    except Exception as e:
        Logger.log_to_frontend(f"  - ❌ 地域選択フローで例外が発生しました: {e}")


async def _dfs_expand_all_nodes(Logger, page: Page, tree_container_locator: Locator, initial_nodes_to_skip_expansion: List[str]):
    """
    Playwright を使用し、ツリーノードをDFS方式で自動展開します。
    JavaScriptを用いた一括展開により処理速度を最適化しています。
    """
    Logger.log_to_frontend(" - ⚙️ 全ツリーノードの自動展開中 (DFS/JS最適化)...")
    expanded_total_count = 0
    
    nodes_to_skip_set = set(initial_nodes_to_skip_expansion)
    
    js_batch_expand_script = r'''
        async (treeContainer, skipNodes) => {
            if (!treeContainer) {
                return 0;
            }

            let clickedInThisBatch = 0;
            let currentIterationClicked = true;
            let safetyCounter = 0;
            const MAX_BATCH_ITERATIONS = 100;

            while (currentIterationClicked && safetyCounter < MAX_BATCH_ITERATIONS) {
                currentIterationClicked = false;
                safetyCounter++;

                const nodes = treeContainer.querySelectorAll('li.filter-node');
                for (const node of nodes) {
                    if (node.offsetParent === null || window.getComputedStyle(node).visibility === 'hidden') {
                        continue;
                    }

                    const titleSpan = node.querySelector('span.qccd-tree-title > div.tree-item-title > span.text-dk');
                    const nodeText = titleSpan ? titleSpan.textContent.trim().replace(/<em><\/em>/g, '') : '';

                    if (skipNodes.includes(nodeText)) {
                        continue;
                    }

                    const switcher = node.querySelector('span.qccd-tree-switcher_close');
                    if (switcher) {
                        switcher.click();
                        clickedInThisBatch++;
                        currentIterationClicked = true;
                        await new Promise(r => setTimeout(r, 20));
                    }
                }
                if (currentIterationClicked) {
                    await new Promise(r => setTimeout(r, 100));
                }
            }
            return clickedInThisBatch;
        }
    '''

    while True:
        Logger.log_to_frontend(" - ブラウザ内でJSスクリプトを実行し、ノードを一括展開しています...")
        tree_handle = await tree_container_locator.element_handle()
        if not tree_handle:
            Logger.log_to_frontend(" - エラー: ツリーコンテナのハンドルを取得できませんでした。")
            break

        clicked_this_batch = await tree_handle.evaluate(js_batch_expand_script, list(nodes_to_skip_set))
        
        if clicked_this_batch == 0:
            Logger.log_to_frontend(f" - ✅ 展開可能なノードはありません。")
            break
        
        expanded_total_count += clicked_this_batch
        Logger.log_to_frontend(f" - バッチ処理完了: {clicked_this_batch} ノード展開。累計: {expanded_total_count}")
        await page.wait_for_timeout(500)

    Logger.log_to_frontend(f" - ✅ 全ノードの展開が完了しました。合計 {expanded_total_count} ノード。")
    return expanded_total_count


async def _batch_check_nodes(Logger, page: Page, tree_container_locator: Locator, nodes_to_check_text: List[str]):
    """
    LLM が指定したノードテキストに基づき、ブラウザ内でJSを用いてチェックボックスを一括選択します。
    """
    Logger.log_to_frontend("  - ⚙️ LLM 指定ノードの一括チェックを実行中...")
    checked_count = 0
    
    check_texts_set = set(nodes_to_check_text)

    js_batch_check_script = r'''
        async (treeContainer, checkTexts) => {
            if (!treeContainer) {
                return 0;
            }

            let clickedCount = 0;
            const nodes = treeContainer.querySelectorAll('li.filter-node');

            for (const node of nodes) {
                if (node.offsetParent === null || window.getComputedStyle(node).visibility === 'hidden') {
                    continue;
                }

                const titleSpan = node.querySelector('span.qccd-tree-title > div.tree-item-title > span.text-dk');
                const nodeText = titleSpan ? titleSpan.textContent.trim().replace(/<em><\/em>/g, '') : '';

                if (checkTexts.includes(nodeText)) {
                    const checkboxInner = node.querySelector('span.qccd-tree-checkbox > span.qccd-tree-checkbox-inner');
                    const isChecked = node.querySelector('span.qccd-tree-checkbox.qccd-tree-checkbox-checked');
                    
                    if (checkboxInner && !isChecked) {
                        checkboxInner.click();
                        clickedCount++;
                        await new Promise(r => setTimeout(r, 20));
                    }
                }
            }
            return clickedCount;
        }
    '''
    
    tree_handle = await tree_container_locator.element_handle()
    if not tree_handle:
        Logger.log_to_frontend("  - エラー: ツリーコンテナのハンドルを取得できませんでした。")
        return 0

    checked_count = await tree_handle.evaluate(js_batch_check_script, list(check_texts_set))
    
    Logger.log_to_frontend(f"  - ✅ 一括チェック完了: {checked_count} ノードを選択しました。")
    sys.stdout.flush()
    return checked_count


async def _collect_all_visible_tree_nodes_data(Logger, tree_container_locator: Locator) -> List[Dict[str, Any]]:
    """
    すべての可視ツリーノードを収集し、depth（階層深度）と top_level_parent（所属する大分類）を付与します。
    """
    Logger.log_to_frontend("  - ⚙️ 可視ツリーノード情報の収集中（階層構造解析含む）...")

    all_nodes_data = []
    
    try:
        tree_container_handle = await tree_container_locator.element_handle()
        if not tree_container_handle:
            return []

        js_collect_script = r'''
        (container) => {
            const results = [];
            const nodes = container.querySelectorAll('li.filter-node');
            
            for (const node of nodes) {
                if (node.offsetParent === null || window.getComputedStyle(node).visibility === 'hidden') {
                    continue;
                }

                const titleSpan = node.querySelector('span.qccd-tree-title > div.tree-item-title > span.text-dk');
                let nodeText = titleSpan ? titleSpan.textContent.trim() : "";
                nodeText = nodeText.replace(/<em><\/em>/g, '');
                if (!nodeText) continue;

                let depth = 0;
                let current = node.parentElement;
                let topLevelName = nodeText;
                
                const path = [];
                path.push(node);

                while (current && !current.matches('ul.qccd-tree')) {
                    if (current.matches('li.filter-node')) {
                        depth++;
                        path.push(current);
                    }
                    current = current.parentElement;
                }
                
                if (path.length > 0) {
                    const rootLi = path[path.length - 1];
                    const rootTitleSpan = rootLi.querySelector('span.qccd-tree-title > div.tree-item-title > span.text-dk');
                    if (rootTitleSpan) {
                        topLevelName = rootTitleSpan.textContent.trim().replace(/<em><\/em>/g, '');
                    }
                }

                const switcher = node.querySelector('span.qccd-tree-switcher');
                let isExpandable = false;
                if (switcher) {
                    const cls = switcher.className || "";
                    if (cls.includes('open') || cls.includes('close')) {
                        if (!cls.includes('noop')) isExpandable = true;
                    }
                }
                
                const checkboxInner = node.querySelector('span.qccd-tree-checkbox > span.qccd-tree-checkbox-inner');
                const isChecked = !!node.querySelector('span.qccd-tree-checkbox.qccd-tree-checkbox-checked');

                results.push({
                    "node_text": nodeText,
                    "depth": depth,
                    "top_level_parent": topLevelName,
                    "is_expandable": isExpandable,
                    "has_checkbox": !!checkboxInner,
                    "is_checked": isChecked
                });
            }
            return results;
        }
        '''
        
        all_nodes_data = await tree_container_handle.evaluate(js_collect_script)
        Logger.log_to_frontend(f"  - ✅ {len(all_nodes_data)} 個の業界ノードを収集しました。")
        return all_nodes_data

    except Exception as e:
        Logger.log_to_frontend(f"  - ツリーノード情報の収集に失敗しました: {e}")
        return []


def _apply_mutual_exclusion_optimization(Logger, full_category_nodes: List[Dict[str, Any]], selected_texts: List[str]) -> List[str]:
    """
    ノードリストに基づき、親子関係における排他ロジックを適用します。
    詳細な子ノードが選択されている場合、高階層の親ノードを除外します。
    """
    if not selected_texts:
        return []

    selected_set = set(selected_texts)
    
    # ロジック 1: 詳細な子ノードを優先（親ノードの選択解除）
    temp_list_for_iteration = list(selected_set)
    for node_text in temp_list_for_iteration:
        node_info = next((n for n in full_category_nodes if n['node_text'] == node_text), None)
        
        if node_info and node_info.get("is_expandable") and node_info['depth'] <= 1:
            current_index = full_category_nodes.index(node_info)
            has_selected_child = False
            
            for i in range(current_index + 1, len(full_category_nodes)):
                subsequent_node = full_category_nodes[i]
                if subsequent_node['depth'] <= node_info['depth']:
                    break
                
                if subsequent_node['node_text'] in selected_set:
                    has_selected_child = True
                    break
            
            if has_selected_child:
                if node_text in selected_set:
                    selected_set.remove(node_text)
                    Logger.log_to_frontend(f"    - ✂️ 排他制御: 子ノードが選択されているため、親ノード '{node_text}' を除外しました。")

    # ロジック 2: 親ノード選択時の子ノード除外
    for i, node in enumerate(full_category_nodes):
        node_text = node['node_text']
        node_depth = node['depth']
        
        if node_text in selected_set and node.get("is_expandable"):
            for j in range(i + 1, len(full_category_nodes)):
                subsequent_node = full_category_nodes[j]
                
                if subsequent_node['depth'] <= node_depth:
                    break
                
                if subsequent_node['node_text'] in selected_set:
                    selected_set.remove(subsequent_node['node_text'])
                    Logger.log_to_frontend(f"    - ✂️ 排他制御: 親ノード '{node_text}' が選択されているため、子ノード '{subsequent_node['node_text']}' を除外しました。")

    return list(selected_set)


def _sanitize_filename(name: str) -> str:
    """ファイル名を無害化します"""
    return re.sub(r'[\\/*?:"<>|]', '_', name).strip()

def _save_industry_cache(Logger, all_nodes: List[Dict[str, Any]]):
    """業界ノードデータを大分類ごとにキャッシュ保存します"""
    Logger.log_to_frontend("  - 💾 業界データを大分類ごとにローカル保存中...")
    
    grouped_data = {}
    top_level_names = []
    
    for node in all_nodes:
        parent = node.get('top_level_parent', '不明な分類')
        if parent not in grouped_data:
            grouped_data[parent] = []
            top_level_names.append(parent)
        grouped_data[parent].append(node)
        
    index_file = os.path.join(INDUSTRY_CACHE_DIR, "top_level_categories.json")
    try:
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(top_level_names, f, ensure_ascii=False, indent=2)
    except Exception as e:
        Logger.log_to_frontend(f"  - ❌ インデックスファイルの保存に失敗しました: {e}")
        
    for category, nodes in grouped_data.items():
        filename = _sanitize_filename(category) + ".json"
        filepath = os.path.join(INDUSTRY_CACHE_DIR, filename)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(nodes, f, ensure_ascii=False, indent=2)
        except Exception as e:
            Logger.log_to_frontend(f"  - ❌ カテゴリ '{category}' の保存に失敗しました: {e}")
            
    Logger.log_to_frontend(f"  - ✅ キャッシュ保存完了。合計 {len(top_level_names)} カテゴリ。")

def _load_top_level_categories(Logger) -> List[str]:
    """大分類インデックスを読み込みます"""
    index_file = os.path.join(INDUSTRY_CACHE_DIR, "top_level_categories.json")
    if os.path.exists(index_file):
        try:
            with open(index_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return []

def _load_nodes_for_category(Logger, category_name: str) -> List[Dict[str, Any]]:
    """指定された大分類のノードデータを読み込みます"""
    filename = _sanitize_filename(category_name) + ".json"
    filepath = os.path.join(INDUSTRY_CACHE_DIR, filename)
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            Logger.log_to_frontend(f"  - ファイル '{filename}' の読み込みに失敗しました: {e}")
    return []

def _check_industry_cache_valid() -> bool:
    """キャッシュの有効性を簡易チェックします"""
    return os.path.exists(os.path.join(INDUSTRY_CACHE_DIR, "top_level_categories.json"))


async def _collect_checkbox_element_data(Logger, page: Page, container_locator: Locator) -> Dict[str, List[Dict[str, Any]]]:
    """
    指定コンテナ内のチェックボックス情報を収集・構造化します。
    無効なデータや取得できない項目はフィルタリングします。
    """
    Logger.log_to_frontend("  - ⚙️ チェックボックス要素データの収集中...")
    structured_data: Dict[str, List[Dict[str, Any]]] = {}
    
    container_handle = await container_locator.element_handle()
    if not container_handle:
        Logger.log_to_frontend("  - エラー: コンテナのハンドルを取得できませんでした。")
        return structured_data

    js_extract_script = r'''
        (container) => {
            const results = [];
            const targetInputs = container.querySelectorAll('input.qccd-checkbox-input:not([style*="display: none"]):not([style*="visibility: hidden"])');
            if (targetInputs.length === 0) {
                return {data: results, num_inputs: 0};
            }

            for (let i = 0; i < targetInputs.length; i++) {
                const input = targetInputs[i];
                let checkboxDetails = {
                    is_checked: input.checked,
                    checkbox_text: "説明を取得できませんでした",
                    parent_title: "大分類タイトルを取得できませんでした"
                };

                const clickContainer = input.closest('.click-container');
                if (clickContainer) {
                    const titleContainer = clickContainer.querySelector('.element-title-container');
                    if (titleContainer) {
                        let text = titleContainer.textContent.trim();
                        text = text.replace(/<em>\s*<\/em>/g, '').trim(); 
                        
                        if (text.length > 50) {
                            const parts = text.split(/[\s\n]+/);
                            if (parts.length > 0) text = parts[0];
                            if (text.length > 50) text = text.substring(0, 48) + "...";
                        }

                        if (text) {
                            checkboxDetails.checkbox_text = text;
                        }
                    }
                }

                let parentTitle = "その他/一般"; 
                const advanceFiltersPanel = input.closest('.advance-filters-panel');
                if (advanceFiltersPanel) {
                    const titleElement = advanceFiltersPanel.querySelector('.advance-panel-title > .title');
                    if (titleElement) {
                        let text = titleElement.textContent.trim();
                        text = text.replace(/<em>\s*<\/em>/g, '').trim();
                        if (text) parentTitle = text;
                    }
                }
                checkboxDetails.parent_title = parentTitle;
                
                results.push(checkboxDetails);
            }
            return {data: results, num_inputs: targetInputs.length};
        }
    '''

    try:
        js_result = await container_handle.evaluate(js_extract_script)
        
        raw_num_inputs = js_result['num_inputs']
        extracted_data = js_result['data']
        
        Logger.log_to_frontend(f"  - {raw_num_inputs} 個の要素を検出、フィルタリングと構造化を開始...")

        valid_count = 0
        ignored_count = 0

        for item in extracted_data:
            text = item["checkbox_text"]
            parent_title = item['parent_title']

            if text == "説明を取得できませんでした" or not text.strip():
                ignored_count += 1
                continue

            if parent_title not in structured_data:
                structured_data[parent_title] = []

            escaped_text = text.replace("'", "\\'") 

            final_selector = (
                f"div.click-container:has(div.element-title-container:has-text('{escaped_text}')) "
                f"> div.element-placeholder "
                f"> label.qccd-checkbox-wrapper "
                f"> span.qccd-checkbox "
                f"> input.qccd-checkbox-input"
            )

            structured_data[parent_title].append({
                "checkbox_text": text,
                "selector": final_selector, 
                "is_checked": item["is_checked"]
            })
            valid_count += 1
            
        Logger.log_to_frontend(f"  - ✅ 処理完了: 有効 {valid_count} 個, 無効 {ignored_count} 個。")
            
    except Exception as e:
        Logger.log_to_frontend(f"  - ❌ データ収集中にエラーが発生しました: {e}")
        return structured_data

    # デバッグ用保存
    try:
        debug_filename = 'collected_checkbox_data_debug.json'
        await asyncio.to_thread(lambda: json.dump(structured_data, open(debug_filename, 'w', encoding='utf-8'), ensure_ascii=False, indent=4))
    except:
        pass

    return structured_data


def _format_structured_data_for_llm(data: Dict[str, List[Dict[str, Any]]]) -> str:
    """
    LLM プロンプト用に構造化データをテキスト形式に整形します。
    """
    text_format = ""
    for category_title, checkboxes in data.items():
        text_format += f'\n"{category_title}": {{\n'
        for cb in checkboxes:
            clean_text = cb["checkbox_text"].replace('\n', ' ').replace('"', "'")
            text_format += f'  "{clean_text}"\n'
        text_format += '}\n'
        
    return text_format.strip()


async def _batch_check_form_checkboxes(Logger, page: Page, check_decisions: Dict[str, List[str]], container_locator: Locator) -> int:
    """
    LLM の判定に基づき、チェックボックスを一括操作します。
    """
    Logger.log_to_frontend("  - ⚙️ フォームチェックボックスの一括操作を実行中...")
    
    items_to_check_keys: Set[str] = set()
    for category, items in check_decisions.items():
        for item_text in items:
            items_to_check_keys.add(f'{category}|{item_text}')

    if check_decisions:
        EXECUTION_SUMMARY["checkboxes"].update(check_decisions)

    if not items_to_check_keys:
        Logger.log_to_frontend("  - 警告: チェックすべき項目がありません。")
        return 0

    all_checkbox_data = await _collect_checkbox_element_data(Logger, page, container_locator)
    
    checked_count = 0
    
    Logger.log_to_frontend(f"  - マッチングとクリック操作を開始...")
    
    for parent_title, checkbox_list in all_checkbox_data.items():
        for item in checkbox_list:
            checkbox_text = item["checkbox_text"]
            is_checked_current = item["is_checked"]
            selector = item["selector"]
            
            key = f'{parent_title}|{checkbox_text}'
            
            if key in items_to_check_keys and not is_checked_current:
                try:
                    target_locator = page.locator(selector)
                    target_count = await target_locator.count()

                    if target_count >= 1:
                        await target_locator.first.check(force=True, timeout=10000)
                        
                        Logger.log_to_frontend(f"      - クリック成功: [{parent_title}] [{checkbox_text}]")
                        checked_count += 1
                        await page.wait_for_timeout(50)
                    else:
                        Logger.log_to_frontend(f"      - ❌ 特定失敗: [{checkbox_text}] (カウント: {target_count})")

                except Exception as e:
                    Logger.log_to_frontend(f"      - ❌ クリック失敗: [{parent_title}] [{checkbox_text}] エラー: {e}")

    Logger.log_to_frontend(f"  - ✅ 一括操作完了: {checked_count} 項目チェック済み。")
    return checked_count

def _get_dropdown_metadata_js():
    """ブラウザ内でドロップダウンメタデータを収集するJSスクリプトを返します。"""
    js_script = """
    (dropdownDivs) => {
        const results = [];
        
        const findCategoryText = (el) => {
            let categoryText = "未分類";
            let clickContainer = el.closest('.click-container');
            
            if (clickContainer) {
                let panel = clickContainer.closest('.advance-filters-panel');
                if (panel) {
                    let panelTitle = panel.querySelector('.advance-panel-title .title');
                    if (panelTitle) {
                        return panelTitle.textContent.trim();
                    }
                }
                let elementTitle = clickContainer.querySelector('.element-title') || clickContainer.querySelector('.drop-down-select-name > span');
                if (elementTitle) {
                    categoryText = elementTitle.textContent.trim();
                }
            } else {
                let panel = el.closest('.advance-filters-panel');
                if (panel) {
                    let panelTitle = panel.querySelector('.advance-panel-title .title');
                    if (panelTitle) {
                        categoryText = panelTitle.textContent.trim();
                    }
                }
            }
            return categoryText.replace(/\\s+/g, ' '); 
        };

        for (const el of dropdownDivs) {
            const ownTextSpan = el.querySelector('span');
            const ownText = ownTextSpan ? ownTextSpan.textContent.trim() : "";
            
            if (!ownText) continue;

            const categoryText = findCategoryText(el);
            
            const selector_id = ownText.replace(/"/g, '\\"').replace(/\\n/g, '').trim(); 
            results.push({
                category_title: categoryText, 
                dropdown_title: ownText,      
                selector: `div.drop-down-select-name.qccd-dropdown-trigger:has-text("${selector_id}")`,
            });
        }
        return results;
    }
    """
    return js_script

async def _collect_dropdown_options_after_hover(Logger, page: Page, selector: str) -> Dict[str, Any]:
    """
    トリガーをクリック後、ドロップダウンメニューのオプションを収集します。
    """
    result = {"dropdown_type": "normal", "options": []}
    
    dropdown_trigger_general = page.locator(selector)
    if await dropdown_trigger_general.count() == 0:
        Logger.log_to_frontend(f"      [DEBUG] トリガーが見つかりません。Selector: {selector}")
        return result

    trigger_element = dropdown_trigger_general.first
        
    try:
        trigger_text = await trigger_element.inner_text()
        Logger.log_to_frontend(f"      [DEBUG] トリガー処理開始: [{trigger_text}]")
        
        await trigger_element.click(timeout=5000)
        await page.wait_for_timeout(300)

        options_root_locator = trigger_element.locator('xpath=..')

        select_items = options_root_locator.locator('.select-item')
        if await select_items.count() > 0:
            result['dropdown_type'] = 'radio'
            structured_options = []
            
            for group_idx in range(await select_items.count()):
                item_locator = select_items.nth(group_idx)
                title_locator = item_locator.locator('.select-title')
                
                if await title_locator.count() > 0:
                    group_title = (await title_locator.inner_text()).strip().replace('\n', ' ')
                else:
                    group_title = "汎用オプション"

                radio_options_locator = item_locator.locator('.radio-item')
                if await radio_options_locator.count() > 0:
                    radio_texts = await radio_options_locator.all_text_contents()
                    for choice_idx, text in enumerate(radio_texts):
                        choice_text = text.strip().replace('\n', ' ')
                        if choice_text:
                            structured_options.append({
                                "group_title": group_title,
                                "group_index": group_idx,
                                "choice": choice_text,
                                "choice_index": choice_idx
                            })
            result['options'] = structured_options
            Logger.log_to_frontend(f"      [DEBUG] {len(result['options'])} 個のRadioオプションを収集しました。")

        if not result['options']:
            ul_options_locator = options_root_locator.locator('ul li')
            ul_count = await ul_options_locator.count()
            if ul_count > 0:
                result['dropdown_type'] = 'normal'
                ul_text_contents = await ul_options_locator.all_text_contents()
                result['options'] = [text.strip().replace('\n', ' ') for text in ul_text_contents if text.strip()]
                Logger.log_to_frontend(f"      [DEBUG] 通常オプション {len(result['options'])} 個を収集しました。")

        try:
            await trigger_element.click()
        except:
            pass
        await page.wait_for_timeout(100)
        return result

    except Exception as e:
        Logger.log_to_frontend(f"      [DEBUG] [{selector}] 処理中に例外: {e}")
        try:
            await trigger_element.click()
        except:
            pass
        return result


async def _prompt_llm_for_dropdown_selection(Logger, dropdown_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    収集されたドロップダウンメニュー情報をLLMに提供し、選択すべき項目を決定させます。
    """
    if not dropdown_data:
        return {"normal_dropdown_selections": [], "radio_dropdown_selections": []}

    data_for_llm = []
    for i, item in enumerate(dropdown_data):
        original_selector = item.get('selector', f"//error/selector[{i}]")
        data_for_llm.append({
            "id": i,
            "category_title": item.get('category_title', ''),
            "dropdown_title": item.get('dropdown_title', ''),
            "type": item.get('dropdown_type', 'normal'),
            "options": item.get('options', []),
            "original_selector": original_selector
        })

    llm_prompt = f"""
    你是一个专业的网页自动化助手兼企业画像专家。你的任务是根据提供的企业筛选条件表单信息和目标指导文本（企业画像），识别出所有需要选择的选项（符合这个企业画像的筛选条件），
    并生成一个有效的 JSON 对象，该对象应包含你决定选择的所有下拉菜单选项。

    **目标指导文本（企业画像）:** "{LLM_GUIDANCE_TEXT}"

下面是下拉菜单列表（每个 radio 选项包含 group_index 和 choice_index）：
{json.dumps(data_for_llm, ensure_ascii=False, indent=2)}

请返回 JSON，格式严格如下（仅返回 JSON 块，不要多余文字）：
(注意：每个选中的选项请严格根据其所属的dropdown_type分类到normal_dropdown_selections或radio_dropdown_selections的其中一类中，不要放错分类！)

{{
  "reason": "（这个字段请用日语填写）简要说明针对高级选项的选择依据",
  "normal_dropdown_selections": [
    {{ "selector": "<与输入中 original_selector 完全匹配>", "selection": "<选中的普通选项文本或空字符串>" }}
  ],
  "radio_dropdown_selections": [
    {{ "selector": "<与输入中 original_selector 完全匹配>", "selections": [
        {{ "choice": "<选项文本>", "group_index": <int>, "choice_index": <int> }},
        ...
      ]
    }}
  ]
}}

注意：
- 对于 type=="radio" 的菜单中选中的选项，你必须在 selections 中返回对象（包含 choice, group_index, choice_index），不要只返回纯字符串。
- group_index 和 choice_index 必须基于页面采集时的顺序（从 0 开始）。
- 如果菜单仅选中“不限”，则等于不做选择。请不要把这个菜单列出来
- 没有选中任何选项的菜单请不要列出来。
- 如果没有选择项，请返回空数组 []。
"""

    raw = await _call_llm_for_decision_json(Logger, llm_prompt)
    if raw is None:
        return {"normal_dropdown_selections": [], "radio_dropdown_selections": []}

    if raw and isinstance(raw, dict):
        EXECUTION_SUMMARY["reasons"]["dropdowns"] = raw.get("reason", "理由なし")
        return raw
    
    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str):
        m = re.search(r'(\{[\s\S]*\})', raw.strip())
        if m:
            try:
                return json.loads(m.group(1))
            except Exception as e:
                Logger.log_to_frontend("❌ JSON 解析エラー:", e)
                return {"normal_dropdown_selections": [], "radio_dropdown_selections": []}
        else:
            Logger.log_to_frontend("❌ 有効な JSON ブロックが見つかりません。")
            return {"normal_dropdown_selections": [], "radio_dropdown_selections": []}

    Logger.log_to_frontend("❌ 不明な戻り値型:", type(raw))
    return {"normal_dropdown_selections": [], "radio_dropdown_selections": []}


async def _apply_dropdown_selection(Logger, page: Page, llm_decision: Dict[str, Any]):
    """
    LLM の決定を適用し、ドロップダウンメニューの選択を実行します。
    """
    if not llm_decision:
        Logger.log_to_frontend("  - LLM の決定が空です。スキップします。")
        return

    normal_decisions = llm_decision.get("normal_dropdown_selections", [])
    radio_decisions = llm_decision.get("radio_dropdown_selections", [])

    valid_normal = [
        d for d in normal_decisions 
        if d.get('selector') and d.get('selection') and str(d.get('selection')).strip() != ""
    ]
    
    valid_radio = [
        d for d in radio_decisions 
        if d.get('selector') and d.get('selections') and len(d.get('selections')) > 0
    ]

    all_decisions = valid_normal + valid_radio
    
    if not all_decisions:
        Logger.log_to_frontend("  - 実行すべきドロップダウン選択操作はありません。")
        return
    
    Logger.log_to_frontend(f"  - 合計 {len(all_decisions)} 個のドロップダウン操作を待機中...")

    for decision in all_decisions:
        selector = decision.get('selector')
        if not selector:
            continue

        dropdown_trigger_general = page.locator(selector)
        if await dropdown_trigger_general.count() == 0:
            Logger.log_to_frontend(f"  - トリガー [{selector}] が見つからないためスキップします。")
            continue

        trigger_element = dropdown_trigger_general.first
        try:
            await trigger_element.click(timeout=5000)
            await page.wait_for_timeout(300)
            
            active_content_container = trigger_element.locator('xpath=..')
            performed = False

            # 通常ドロップダウン処理
            if 'selection' in decision:
                selected_text = decision.get('selection', '').strip()
                if selected_text:
                    try:
                        container_handle = None
                        try:
                            container_handle = await active_content_container.element_handle()
                        except Exception:
                            container_handle = None

                        if not container_handle:
                            Logger.log_to_frontend(f"  - エラー: ElementHandle 取得失敗。[{selector}]")
                        else:
                            js_click_script = r'''
                                (container, text) => {
                                    try {
                                        if (!container) return {clicked: false};
                                        const lis = container.querySelectorAll('ul li');
                                        if (!lis || lis.length === 0) return {clicked: false};
                                        const needle = String(text).trim();
                                        for (let i = 0; i < lis.length; i++) {
                                            const li = lis[i];
                                            const txt = (li.textContent || "").replace(/\s+/g, ' ').trim();
                                            if (txt && txt.indexOf(needle) !== -1) {
                                                const clickable = li.querySelector('a, button, label, span') || li;
                                                try { clickable.click(); return {clicked: true}; } catch(e) {}
                                                try { li.click(); return {clicked: true}; } catch(e2) {}
                                                return {clicked: false};
                                            }
                                        }
                                        return {clicked: false};
                                    } catch (err) {
                                        return {clicked: false, error: String(err)};
                                    }
                                }
                            '''
                            res = await container_handle.evaluate(js_click_script, selected_text) 

                            if isinstance(res, dict) and res.get('clicked'):
                                Logger.log_to_frontend(f"  - 通常ドロップダウン選択: {selected_text}")
                                performed = True

                                menu_name = "汎用メニュー"
                                selector_str = decision.get('selector', '')
                                match = re.search(r'has-text\("([^"]+)"\)', selector_str)
                                if match:
                                    menu_name = match.group(1)
                                    
                                selected_values = []
                                if 'selection' in decision:
                                    val = decision.get('selection', '').strip()
                                    if val: selected_values.append(val)
                                elif 'selections' in decision:
                                    for s in decision.get('selections', []):
                                        if isinstance(s, dict):
                                            selected_values.append(s.get('choice', ''))
                                
                                if selected_values:
                                    if "dropdowns" not in EXECUTION_SUMMARY: EXECUTION_SUMMARY["dropdowns"] = {}
                                    
                                    cat_key = "詳細オプション" 
                                    if cat_key not in EXECUTION_SUMMARY["dropdowns"]:
                                        EXECUTION_SUMMARY["dropdowns"][cat_key] = {}
                                        
                                    EXECUTION_SUMMARY["dropdowns"][cat_key][menu_name] = selected_values

                            else:
                                Logger.log_to_frontend(f"  - オプションが見つからないかクリックに失敗しました: {selected_text}")

                    except Exception as e:
                        Logger.log_to_frontend(f"  - 通常ドロップダウン処理例外: {e}")

            # Radio ドロップダウン処理
            if 'selections' in decision:
                sels = decision.get('selections', [])
                for sel in sels:
                    if isinstance(sel, dict) and 'group_index' in sel and 'choice_index' in sel:
                        gi = int(sel['group_index'])
                        ci = int(sel['choice_index'])
                        group_locator = active_content_container.locator('.select-item').nth(gi)
                        choice_locator = group_locator.locator('.radio-item').nth(ci)
                        
                        input_locator = choice_locator.locator('input.qccd-radio-input')
                        if await input_locator.count() > 0:
                            await input_locator.first.click(timeout=500)
                            performed = True
                        elif await choice_locator.count() > 0:
                            await choice_locator.first.click(timeout=3000)
                            performed = True
                        else:
                            Logger.log_to_frontend(f"  - Radio 位置特定失敗 (gi={gi}, ci={ci})。")

            await page.wait_for_timeout(200) 
            
            is_dropdown_still_visible = False
            try:
                dropdown_body = active_content_container.locator('.qccd-dropdown')
                if await dropdown_body.count() > 0 and await dropdown_body.first.is_visible():
                    is_dropdown_still_visible = True
            except:
                is_dropdown_still_visible = False 

            if is_dropdown_still_visible:
                try:
                    await trigger_element.click(timeout=2000)
                except:
                    pass

            await page.wait_for_timeout(100)

        except Exception as e:
            Logger.log_to_frontend(f"  - ドロップダウン操作エラー: {e}")
            try:
                if await trigger_element.is_visible(): 
                     await trigger_element.click(timeout=1000)
            except:
                pass
            continue

    Logger.log_to_frontend("✅ ドロップダウン選択操作完了。")
    sys.stdout.flush()
    await page.wait_for_timeout(300)


async def _collect_and_apply_dropdown_filters(Logger, page: Page):
    """
    ドロップダウンメニューフィルタを収集、判定、適用します（キャッシュ対応）。
    """
    dropdown_cache_file = "dropdown_complete_data.json"
    complete_dropdown_data = _load_from_cache(Logger, dropdown_cache_file)
    
    if complete_dropdown_data is not None:
         Logger.log_to_frontend(f"  - ⏩ キャッシュ検出。ドロップダウン収集処理をスキップします。")
    else:
        container_locator = page.locator('div.advance-filters-container')
        dropdown_triggers_locator = container_locator.locator(
            'div.drop-down-select-name.qccd-dropdown-trigger:not(.is-multi)'
        )
        
        count = await dropdown_triggers_locator.count()
        if count == 0:
            Logger.log_to_frontend(" - ターゲットとなるドロップダウンメニューが見つかりませんでした。")
            return

        Logger.log_to_frontend(f"\n📢 {count} 個のドロップダウンメニューを検出。メタデータ収集中...")
        initial_metadata = await dropdown_triggers_locator.evaluate_all(_get_dropdown_metadata_js())
        
        if not initial_metadata:
            return
        
        complete_dropdown_data = []
        Logger.log_to_frontend(f"⚙️ {len(initial_metadata)} 個のメニューオプションを収集中...")

        for i, item in enumerate(initial_metadata):
            Logger.log_to_frontend(f"  - 処理中 {i+1}/{len(initial_metadata)}: [{item['dropdown_title']}]")
            options_info = await _collect_dropdown_options_after_hover(Logger, page, item['selector'])
            item.update(options_info)
            if item['options']:
                complete_dropdown_data.append(item)
            else:
                Logger.log_to_frontend(f"    - 警告: [{item['dropdown_title']}] のオプションが空です。スキップします。")
        
        if complete_dropdown_data:
            _save_to_cache(Logger, dropdown_cache_file, complete_dropdown_data)

    if not complete_dropdown_data:
        Logger.log_to_frontend(" - 有効なドロップダウン情報がありません。スキップします。")
        return

    llm_decision = await _prompt_llm_for_dropdown_selection(Logger, complete_dropdown_data)
    await _apply_dropdown_selection(Logger, page, llm_decision)


async def _collect_special_multi_select_data(Logger, page: Page) -> List[Dict[str, Any]]:
    """
    特殊構造の多肢選択ドロップダウンメニューデータを対話的に収集します。
    """
    Logger.log_to_frontend("  - ⚙️ 特殊多肢選択ドロップダウンデータの収集中（高速モード）...")
    
    results = []

    target_selector = '.advance-filters-panel.advance-panel-sub-line .drop-down-select-name.is-multi.qccd-dropdown-trigger'
    triggers = page.locator(target_selector)
    count = await triggers.count()
    
    if count == 0:
        Logger.log_to_frontend("  - ターゲットが見つかりません。")
        return []

    Logger.log_to_frontend(f"  - {count} 個のターゲットメニューを発見。スキャンを開始します...")

    for i in range(count):
        try:
            trigger = triggers.nth(i)
            
            panel = trigger.locator('xpath=./ancestor::div[contains(@class, "advance-filters-panel")][1]')
            category_title = "その他"
            if await panel.count() > 0:
                title_el = panel.locator('.advance-panel-title .title')
                if await title_el.count() > 0:
                    category_title = await title_el.text_content()
                    category_title = category_title.strip()

            text_span = trigger.locator('span').first
            dropdown_title = await text_span.text_content() if await text_span.count() > 0 else await trigger.text_content()
            dropdown_title = dropdown_title.strip()

            await trigger.scroll_into_view_if_needed()
            
            wrapper = trigger.locator('xpath=./ancestor::div[contains(@class, "adv-common-select") or contains(@class, "adv-common-cascader")][1]')
            dropdown_content = wrapper.locator('.qccd-dropdown')

            if await trigger.is_visible():
                await trigger.hover() 
                await trigger.click()
                
                try:
                    await dropdown_content.wait_for(state='visible', timeout=1500)
                except Exception:
                    await trigger.click()
                    await page.wait_for_timeout(200)
            else:
                continue

            cascader_levels = wrapper.locator('.dropdown-level')
            target_container = cascader_levels.first if await cascader_levels.count() > 0 else (
                wrapper.locator('.select-container') if await wrapper.locator('.select-container').count() > 0 else wrapper
            )

            options_locator = target_container.locator('li[title]')
            if await options_locator.count() > 0:
                raw_texts = await options_locator.evaluate_all("list => list.map(el => el.getAttribute('title'))")
                option_texts = [t.strip() for t in raw_texts if t and t.strip()]
            else:
                raw_texts = await target_container.locator('li').all_text_contents()
                option_texts = [t.strip() for t in raw_texts if t.strip()]

            if option_texts:
                results.append({
                    "category_title": category_title,
                    "dropdown_title": dropdown_title,
                    "options": option_texts,
                    "trigger_index": i, 
                    "selector": target_selector,
                    "is_cascader": await cascader_levels.count() > 0
                })
            
            Logger.log_to_frontend(f"    - [{i+1}/{count}] {dropdown_title}: {len(option_texts)} 項目取得")

            await page.mouse.move(0, 0)
            await page.mouse.click(0, 0)

            try:
                await dropdown_content.wait_for(state='hidden', timeout=1000)
            except:
                if await dropdown_content.is_visible():
                    await trigger.click()
                    await page.mouse.move(0, 0)
            
            await page.wait_for_timeout(50) 

        except Exception as e:
            Logger.log_to_frontend(f"    - ⚠️ スキャン警告: {e}")
            await page.mouse.click(0, 0)
            continue

    return results


async def _apply_special_multi_select_decisions(Logger, page: Page, data: List[Dict[str, Any]]):
    """
    LLM に特殊多肢選択メニューの判定を行わせ、操作を実行します。
    """
    if not data:
        return

    prompt_data_str = json.dumps([{
        "id": i,
        "大类": item["category_title"],
        "菜单名称": item["dropdown_title"],
        "可选项": item["options"]
    } for i, item in enumerate(data)], ensure_ascii=False, indent=2)

    llm_prompt = f"""
    你是一个企业筛选专家。请根据目标企业画像，从以下多选下拉菜单中选择需要勾选的选项。
    
    **目标企业画像:** "{LLM_GUIDANCE_TEXT}"
    
    **待选菜单列表:**
    {prompt_data_str}
    
    请返回一个 JSON 对象，格式如下：
    {{
        "decisions": [
            {{
                "id": <对应列表中的id, 整数>,
                "selected_options": ["<选项1>", "<选项2>"] 
            }}
        ]
    }}
    
    注意：
    1. `selected_options` 必须是“可选项”列表中精确存在的字符串。
    2. 如果某个菜单不需要勾选任何项，请不要将其包含在 `decisions` 数组中。
    3. 仅返回 JSON。
    """

    Logger.log_to_frontend("  - 特殊多肢選択メニューのチェックについて LLM に意思決定を依頼中...")
    llm_result = await _call_llm_for_decision_json(Logger, llm_prompt)

    if not llm_result or "decisions" not in llm_result:
        Logger.log_to_frontend("  - 有効な決定が得られませんでした。スキップします。")
        return

    decisions = llm_result["decisions"]
    Logger.log_to_frontend(f"  - {len(decisions)} 個のメニュー操作を決定しました。")

    for decision in decisions:
        try:
            idx = decision.get("id")
            targets = decision.get("selected_options", [])
            if idx is None or not targets or idx >= len(data):
                continue

            menu_info = data[idx]
            trigger_index = menu_info.get("trigger_index") 
            base_selector = menu_info.get("selector")
            
            if trigger_index is None:
                continue

            Logger.log_to_frontend(f"    - 操作中: {menu_info['dropdown_title']} ({len(targets)} 項目)")

            triggers = page.locator(base_selector)
            trigger = triggers.nth(trigger_index)

            try:
                await trigger.scroll_into_view_if_needed(timeout=2000)
            except:
                pass

            if not await trigger.is_visible():
                 Logger.log_to_frontend("      ❌ トリガーが不可視です。スキップします。")
                 continue

            try:
                await trigger.hover(timeout=1000)
            except:
                pass 
            
            await trigger.evaluate("el => el.click()")
            
            wrapper = trigger.locator('xpath=./ancestor::div[contains(@class, "adv-common-select") or contains(@class, "adv-common-cascader")][1]')
            dropdown_content = wrapper.locator('.qccd-dropdown')

            try:
                await dropdown_content.wait_for(state='visible', timeout=2000)
            except:
                try:
                    await trigger.click(timeout=1000)
                except:
                    pass

            if menu_info.get("is_cascader", False):
                 target_scope = wrapper.locator('.dropdown-level').first
            else:
                 select_container = wrapper.locator('.select-container')
                 if await select_container.count() > 0:
                     target_scope = select_container
                 else:
                     target_scope = wrapper

            for opt_text in targets:
                target_li = target_scope.locator(f"li[title='{opt_text}']")
                if await target_li.count() == 0:
                    target_li = target_scope.locator(f"li:has-text('{opt_text}')").first
                
                if await target_li.count() > 0:
                    checkbox = target_li.locator(".qccd-checkbox-input")
                    if await checkbox.count() > 0:
                        if not await checkbox.is_checked():
                            await checkbox.evaluate("el => el.click()")
                            Logger.log_to_frontend(f"      - [JS] チェック済み: {opt_text}")
                    else:
                        await target_li.evaluate("el => el.click()")
                        Logger.log_to_frontend(f"      - [JS] クリック済み: {opt_text}")
                else:
                     Logger.log_to_frontend(f"      ⚠️ オプションが見つかりません: {opt_text}")

            await page.wait_for_timeout(100)

            await page.mouse.move(0, 0)

            try:
                await trigger.evaluate("el => el.click()")
            except:
                pass

            await page.wait_for_timeout(300)
            is_visible = await dropdown_content.is_visible()

            if is_visible:
                Logger.log_to_frontend("      ⚠️ メニューが閉じません。強制非表示を実行します。")
                await dropdown_content.evaluate("el => el.style.display = 'none'")
            
            await page.wait_for_timeout(200)

        except Exception as e:
            Logger.log_to_frontend(f"      ❌ 操作例外: {e}")
            try:
                wrapper = triggers.nth(trigger_index).locator('xpath=./ancestor::div[contains(@class, "adv-common-select")][1]')
                dropdown_content = wrapper.locator('.qccd-dropdown')
                await dropdown_content.evaluate("el => el.style.display = 'none'")
            except:
                pass

        category_title = menu_info.get("category_title", "その他")
        dropdown_title = menu_info.get("dropdown_title", "不明なメニュー")
        
        if "dropdowns" not in EXECUTION_SUMMARY:
            EXECUTION_SUMMARY["dropdowns"] = {}
            
        if category_title not in EXECUTION_SUMMARY["dropdowns"]:
            EXECUTION_SUMMARY["dropdowns"][category_title] = {} 

        EXECUTION_SUMMARY["dropdowns"][category_title][dropdown_title] = targets

        Logger.log_to_frontend("  - ✅ 特殊多肢選択メニュー操作完了。")


async def test_qcc_llm_interaction_with_playwright(Logger, client_description: str):
    """
    Playwright と LLM を連携させた自動化テストのメインフローです。
    """
    global EXECUTION_SUMMARY
    EXECUTION_SUMMARY = {
        "keywords": [],
        "regions": [],
        "checkboxes": {}, 
        "dropdowns": {},
        "industry_tree": [],
        "reasons": {}
    }

    Logger.log_to_frontend("🚀 クラウドブラウザを起動中...")
    
    p = await async_playwright().start() 
    browser: Browser = await p.chromium.launch(headless=False)
    page: Page = await browser.new_page()

    target_url = "https://www.qcc.com/web/search/advance?hasState=true"
    Logger.log_to_frontend(f"  - ナビゲート先: {target_url}")
    await page.goto(target_url)

    await _capture_and_send_screenshot(Logger, page, "ページロード完了")

    await page.wait_for_load_state("domcontentloaded")
    Logger.log_to_frontend("  - DOMContentLoaded 到達。")
    await page.wait_for_timeout(2000)

    # ログインポップアップの処理
    Logger.log_to_frontend("  - ログインポップアップの処理を試行...")
    login_modal_close_button: Locator = page.locator("span.qcc-login-modal-close")
    if await login_modal_close_button.is_visible():
        await login_modal_close_button.click()
        Logger.log_to_frontend("  - ログインポップアップを閉じました。")
        await page.wait_for_timeout(2000)
    else:
        Logger.log_to_frontend("  - ログインポップアップの閉じるボタンが見つからないため、スキップします。")

    advance_filters_container = page.locator('.advance-filters-container')
    if not await advance_filters_container.is_visible():
        Logger.log_to_frontend("エラー: '.advance-filters-container' が見つかりません。")
        return

    handle = await advance_filters_container.element_handle()
    advance_filters_html_snippet = await handle.evaluate("el => el.outerHTML")
    
    if not advance_filters_html_snippet:
        Logger.log_to_frontend("エラー: HTML スニペットを取得できません。")
        return
    
    try:
        await asyncio.to_thread(lambda: open('advance_filters_container_html.log', 'w', encoding='utf-8').write(advance_filters_html_snippet))
        Logger.log_to_frontend("  - HTML スニペットをログファイルに保存しました。")
    except Exception as e:
        Logger.log_to_frontend(f"ファイル書き込みエラー: {e}")

    # --- フェーズ 1: キーワード入力 ---
    Logger.log_to_frontend("\n🔍 **フェーズ 1: キーワードのLLM決定と入力**")
    sys.stdout.flush()
    
    input_elements_data = await _collect_targeted_input_element_data(Logger, page, target_placeholder="输入关键词", target_class="qccd-input")
    Logger.log_to_frontend(f"  - ターゲット要素検出: {len(input_elements_data)} 件")

    if not input_elements_data:
        Logger.log_to_frontend("  - ターゲット要素が見つかりません。スキップします。")
    else:
        target_input_selector = input_elements_data[0]['selector'] if input_elements_data else None

        if not target_input_selector:
            Logger.log_to_frontend("  - エラー: 有効なセレクタが取得できません。")
        else:
            llm_fill_keyword_prompt = f"""
            你是一个专业的网页表单填充助手。你的任务是根据提供的目标指导文本，生成3-10个最相关的关键词，并解释原因。
            这个关键词将用于填充网页上 `placeholder="输入关键词"` 且 `class="qccd-input"` 的输入框。

            **目标指导文本:** "{LLM_GUIDANCE_TEXT}"

            请返回如下 JSON 格式：
            {{
                "reason": "（这个字段请用日语填写）简要说明为什么要用这几个关键词",
                "keywords": "关键词1、关键词2、关键词3" 
            }}
            注意：keywords 字段只返回一个字符串，多个词用顿号分隔。
            """

            Logger.log_to_frontend("  - キーワードについてLLMに問い合わせ中...")
            sys.stdout.flush()
            keyword_result = await _call_llm_for_decision_json(Logger, llm_fill_keyword_prompt)

            keyword_to_fill = ""
            if keyword_result and isinstance(keyword_result, dict):
                keyword_to_fill = keyword_result.get("keywords", "").strip()
                EXECUTION_SUMMARY["reasons"]["keywords"] = keyword_result.get("reason", "理由なし")
                if keyword_to_fill:
                    try:
                        target_input = page.locator(target_input_selector)
                        await target_input.focus()
                        await target_input.fill(keyword_to_fill)
                        Logger.log_to_frontend(f"    - 入力成功: '{keyword_to_fill}'")
                        await page.wait_for_timeout(1000)
                    except Exception as e:
                        Logger.log_to_frontend(f"    - 入力エラー: {e}")
                else:
                    Logger.log_to_frontend("  - LLMからのキーワードが空です。")
            else:
                Logger.log_to_frontend("  - LLMから有効なキーワードが得られませんでした。")


    if keyword_to_fill:
        EXECUTION_SUMMARY["keywords"] = [keyword_to_fill]
        await _capture_and_send_screenshot(Logger, page, "キーワード入力完了")

    Logger.log_to_frontend("  - ログインポップアップの再確認...")
    sys.stdout.flush()
    login_modal_close_button: Locator = page.locator("span.qcc-login-modal-close")
    if await login_modal_close_button.is_visible():
        await login_modal_close_button.click()
        Logger.log_to_frontend("  - ログインポップアップを閉じました。")
        await page.wait_for_timeout(2000)
    else:
        Logger.log_to_frontend("  - ポップアップなし。")

    # --- フェーズ 1.5: 地域選択 ---
    await _handle_region_selection(Logger, page) 

    # --- フェーズ 2: チェックボックス選択 ---
    Logger.log_to_frontend("\n🔍 **フェーズ 2: チェックボックスのLLM決定と一括適用**")
    sys.stdout.flush()

    checkbox_cache_file = "checkbox_data.json"
    checkbox_data = _load_from_cache(Logger, checkbox_cache_file)
    
    if checkbox_data is None:
        checkbox_data = await _collect_checkbox_element_data(Logger, page, advance_filters_container)
        if checkbox_data:
            _save_to_cache(Logger, checkbox_cache_file, checkbox_data)
    
    if not checkbox_data:
        Logger.log_to_frontend("  - チェックボックスデータがありません。スキップします。")
    else:
        formatted_checkbox_data = _format_structured_data_for_llm(checkbox_data)
        
        checkbox_decision_prompt = f"""
        你是一个专业的网页自动化助手兼企业画像专家。你的任务是根据提供的企业筛选条件表单信息和目标指导文本（企业画像），识别出所有需要勾选的复选框（符合这个企业画像的筛选条件）。
        
        **目标指导文本（企业画像）:** "{LLM_GUIDANCE_TEXT}"
        
        **以下是企业筛选条件的表单信息，包含大类和其下的所有可选项:**
        {formatted_checkbox_data}
        
        请仔细阅读以上列表和目标指导文本。你的决策是返回一个 JSON 对象，其结构必须完全模仿上面的表单信息结构，但只包含你需要勾选的选项。如果某个大类下所有选项都不需要勾选，则可以不包含该大类或返回空列表。
        注意：对于“无失信被执行人”、“无被执行人”、“无限制高消费”、“无行政处罚”、“无环保处罚”的选项，如果你认为企业画像【有】失信被执行人/被执行人/限制高消费/行政处罚/环保处罚，就不应勾选对应的选项。

        请返回一个 JSON 对象，结构如下：
        {{
            "reason": "（这一段请用日语填写）简要分析为何选择这些状态或条件（例如：根据画像排除有风险的企业）",
            "decision": {{
                "公司状态": ["存续", "在业"],
                "注册资本": ["1亿以上"]
                // ... 其他大类
            }}
        }}
        """

        Logger.log_to_frontend("  - チェック項目についてLLMに問い合わせ中...")
        sys.stdout.flush()
        result_json = await _call_llm_for_decision_json(Logger, checkbox_decision_prompt)
        
        llm_check_decisions = {}
        if result_json and isinstance(result_json, dict):
            llm_check_decisions = result_json.get("decision", {})
            EXECUTION_SUMMARY["reasons"]["checkboxes"] = result_json.get("reason", "理由なし")    

        if llm_check_decisions and isinstance(llm_check_decisions, dict):
            try:
                await asyncio.to_thread(lambda: open('llm_checkbox_decisions.json', 'w', encoding='utf-8').write(json.dumps(llm_check_decisions, ensure_ascii=False, indent=4)))
                Logger.log_to_frontend("  - ✅ LLM決定をログファイルに保存しました。")
            except Exception as e:
                Logger.log_to_frontend(f"  - 警告: ログ保存エラー: {e}")

            Logger.log_to_frontend(f"  - 一括チェックを開始します...")
            await _batch_check_form_checkboxes(Logger, page, llm_check_decisions, advance_filters_container)
            await _capture_and_send_screenshot(Logger, page, "チェックボックスフィルタ完了")
        else:
            Logger.log_to_frontend("  - 有効な決定が得られませんでした。スキップします。")


    # --- フェーズ 3: 特殊構造多肢選択メニュー ---
    Logger.log_to_frontend("\n🔍 **フェーズ 3: 特殊構造多肢選択メニューの処理**")
    
    special_multi_cache_file = "special_multi_select_data.json"
    special_multi_data = _load_from_cache(Logger, special_multi_cache_file)

    if special_multi_data is None:
        special_multi_data = await _collect_special_multi_select_data(Logger, page)
        if special_multi_data:
            _save_to_cache(Logger, special_multi_cache_file, special_multi_data)
    
    if special_multi_data:
        await _apply_special_multi_select_decisions(Logger, page, special_multi_data)
        await _capture_and_send_screenshot(Logger, page, "ドロップダウンメニューフィルタ完了")
    else:
        Logger.log_to_frontend("  - 特殊多肢選択メニューが見つかりません。")

    # --- フェーズ 3.5: ドロップダウン一括操作 ---
    await _collect_and_apply_dropdown_filters(Logger, page)
    await _capture_and_send_screenshot(Logger, page, "ドロップダウンメニューフィルタ完了")

    # --- フェーズ 4: 業界フィルタ ---
    Logger.log_to_frontend("\n🔍 **フェーズ 4: 所属業界フィルタモーダルの操作**")
    sys.stdout.flush()
    
    selector_to_open_industry_modal = "div.into-one-item:has-text('所属行业') .trigger-container"
    
    try:
        target_trigger = page.locator(selector_to_open_industry_modal)
        if await target_trigger.is_visible():
            await target_trigger.click()
            Logger.log_to_frontend(f"  - モーダルを開きました。")
            await page.wait_for_selector('.app-nmodal.modal.fade.pro-tree-modal.in', state='visible', timeout=10000)
            await page.wait_for_timeout(2000)
        else:
            Logger.log_to_frontend(f"  - エラー: トリガーが見つかりません。")
            return
    except Exception as e:
        Logger.log_to_frontend(f"  - エラー: モーダル展開操作で例外が発生しました: {e}")
        return

    Logger.log_to_frontend("\n🔍 **フェーズ 4-2: 業界ノードの展開と選択**")
    sys.stdout.flush()
    modal_locator = page.locator('.app-nmodal.modal.fade.pro-tree-modal.in')

    if not await modal_locator.is_visible():
        Logger.log_to_frontend("  - エラー: モーダルが可視状態ではありません。")
        return

    tree_container = modal_locator.locator('ul.qccd-tree')
    
    Logger.log_to_frontend("  - ステップ A: 全ノードの展開 (DFS)...")
    await _dfs_expand_all_nodes(Logger, page, tree_container, []) 

    cache_valid = _check_industry_cache_valid()
    
    if cache_valid:
        Logger.log_to_frontend("  - ⏩ キャッシュを検出しました。収集をスキップします。")
    else:
        Logger.log_to_frontend("  - 📝 ノード情報の収集を開始...")
        all_nodes_data = await _collect_all_visible_tree_nodes_data(Logger, tree_container)
        if all_nodes_data:
            await asyncio.to_thread(_save_industry_cache, Logger, all_nodes_data)
        else:
            Logger.log_to_frontend("  - ❌ 収集に失敗しました。中断します。")
            return

    # 第1ラウンド LLM: 大分類選定
    Logger.log_to_frontend("\n🧠 **ステップ B: LLM 第1次判定 - 大分類の選定**")
    
    top_level_cats = await asyncio.to_thread(_load_top_level_categories, Logger)
    if not top_level_cats:
        Logger.log_to_frontend("  - ❌ 大分類インデックスのロードに失敗しました。")
        return

    top_level_prompt = f"""
    你是一个企业画像分析专家。请根据目标企业画像，从以下【行业大类】列表中，筛选出**最可能包含目标企业**的大类。
    
    **目标企业画像:** "{LLM_GUIDANCE_TEXT}"
    
    **行业大类列表:**
    {json.dumps(top_level_cats, ensure_ascii=False)}
    
    请返回 JSON 格式：
    {{
        "reason": "（这一个字段请用日语填写）分析理由",
        "selected_categories": ["制造业", "信息传输、软件和信息技术服务业"] 
    }}
    如果不确定或觉得所有都可能，请谨慎选择最相关的。如果均不相关返回空数组。
    """
    
    top_level_result = await _call_llm_for_decision_json(Logger, top_level_prompt)
    target_categories = []
    if top_level_result and isinstance(top_level_result, dict):
        target_categories = top_level_result.get("selected_categories", [])
        reason = top_level_result.get("reason", "")
        Logger.log_to_frontend(f"  - 第1次結果: {len(target_categories)} 大分類を選択。理由: {reason}")
        if reason:
             EXECUTION_SUMMARY["reasons"]["industry_top_level"] = reason
    else:
        Logger.log_to_frontend("  - 有効な結果が得られませんでした。")

    final_nodes_to_check_text = []

    # 第2ラウンド LLM: 詳細ノード選定
    if target_categories:
        Logger.log_to_frontend("\n🧠 **ステップ C: LLM 第2次判定 - 詳細ノードの選定**")
        
        for cat in target_categories:
            Logger.log_to_frontend(f"  - 📂 処理中: 【{cat}】")
            
            cat_nodes = await asyncio.to_thread(_load_nodes_for_category, Logger, cat)
            
            if not cat_nodes:
                Logger.log_to_frontend(f"    - 警告: キャッシュが見つかりません。")
                continue
            
            checkable_options = [n['node_text'] for n in cat_nodes if n.get('has_checkbox') and not n.get('is_checked')]
            
            if not checkable_options:
                Logger.log_to_frontend("    - 利用可能なオプションがありません。")
                continue

            current_cat_selected_texts = [] 
            
            BATCH_SIZE = getattr(globals(), 'BATCH_SIZE_FOR_LLM_SELECTION', 300)
            num_chunks = (len(checkable_options) + BATCH_SIZE - 1) // BATCH_SIZE
            
            for i in range(num_chunks):
                start = i * BATCH_SIZE
                end = min((i + 1) * BATCH_SIZE, len(checkable_options))
                batch_options = checkable_options[start:end]
                
                prompt_options_str = "\n".join(batch_options)
                
                detail_prompt = f"""
                你是一个行业细分专家。目标是在大类“{cat}”下，精确勾选符合画像的细分行业。
                
                **目标企业画像:** "{LLM_GUIDANCE_TEXT}"
                
                **待选细分行业列表:**
                {prompt_options_str}
                
                请返回 JSON：
                {{
                   
                    "selected_nodes": ["细分行业A", "细分行业B"]
                }}
                如果本批次无相关行业，selected_nodes 返回 []。
                """
                
                res = await _call_llm_for_decision_json(Logger, detail_prompt)
                if res and isinstance(res, dict):
                    selected = res.get("selected_nodes", [])
                    valid_selected = [s for s in selected if s in batch_options]
                    current_cat_selected_texts.extend(valid_selected)
                    
                    if res.get("reason"):
                        key = f"industry_{cat}"
                        prev = EXECUTION_SUMMARY["reasons"].get(key, "")
                        EXECUTION_SUMMARY["reasons"][key] = (prev + " " + res.get("reason")).strip()
                        
                    Logger.log_to_frontend(f"    - バッチ {i+1}/{num_chunks}: {len(valid_selected)} 件選択。")
                
                await asyncio.sleep(0.5)

            if current_cat_selected_texts:
                Logger.log_to_frontend(f"    - ⚡ 排他ロジック適用中: 【{cat}】")
                optimized_selection = _apply_mutual_exclusion_optimization(Logger, cat_nodes, current_cat_selected_texts)
                Logger.log_to_frontend(f"    - ✅ 最適化完了: {len(optimized_selection)} ノード (元 {len(current_cat_selected_texts)} ノード)。")
                
                final_nodes_to_check_text.extend(optimized_selection)
    
    if final_nodes_to_check_text:
        final_nodes_to_check_text = list(set(final_nodes_to_check_text))
        Logger.log_to_frontend(f"\n⚙️ **ステップ D: 一括チェック実行 (計 {len(final_nodes_to_check_text)} 項目)...**")
        
        EXECUTION_SUMMARY["industry_tree"] = final_nodes_to_check_text
        
        await _batch_check_nodes(Logger, page, tree_container, final_nodes_to_check_text)
        await _capture_and_send_screenshot(Logger, page, "業界選択完了")
    else:
        Logger.log_to_frontend("  - 選択対象の業界オプションはありませんでした。")

    
    # --- フェーズ 5: 確定処理 ---
    Logger.log_to_frontend("\n⚙️ 完了処理: オプションの保存")
    confirm_button_selector = "div.app-nmodal.modal.fade.pro-tree-modal.in div.modal-footer div.btn.btn-primary:has-text('确定')"
    confirm_button = page.locator(confirm_button_selector)
    if await confirm_button.is_visible():
        await confirm_button.click()
        Logger.log_to_frontend("  - 「確定」ボタンをクリックしました。")
        await page.wait_for_timeout(2000)
    else:
        Logger.log_to_frontend("  - エラー: 「確定」ボタンが見つかりません。")
        modal_close_button = modal_locator.locator("a.nclose")
        if await modal_close_button.is_visible():
            await modal_close_button.click()
            Logger.log_to_frontend("  - 代替処理として「閉じる」ボタンをクリックしました。")
            await page.wait_for_timeout(1000)

    # === 最終レポート生成 ===
    Logger.log_to_frontend("📸 最終スクリーンショットを生成中...")
    try:
        await page.wait_for_timeout(1000)
        full_screenshot = await page.screenshot(full_page=True)
        full_b64 = base64.b64encode(full_screenshot).decode('utf-8')
        Logger.log_to_frontend(f"[SCREENSHOT]{full_b64}") 
    except Exception as e:
        Logger.log_to_frontend(f"スクリーンショット生成失敗: {e}")

    final_text_report = _generate_final_report()
    Logger.log_to_frontend(f"[FINAL_REPORT]{final_text_report}")
    
    Logger.log_to_frontend("✅ テストケースの実行が完了しました。")

    return