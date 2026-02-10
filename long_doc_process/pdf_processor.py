import os
import json
import logging
import time
import shutil
import re  # 正規表現モジュールを追加
from pypdf import PdfReader, PdfWriter
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult

# ================= 設定エリア =================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
KEY_FILE_PATH = os.path.join(PROJECT_ROOT, "api_keys.json")
INPUT_DIR = os.path.join(SCRIPT_DIR, "data_input")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "data_output")
TEMP_DIR = os.path.join(SCRIPT_DIR, "temp_split_files") # 一時ファイル置き場

# 処理する最大ページ数（ここを10や20に変更可能）
# None にすると全ページを処理しますが、時間がかかります。
TARGET_MAX_PAGES = 230

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_azure_config():
    if not os.path.exists(KEY_FILE_PATH):
        raise FileNotFoundError(f"設定ファイルが見つかりません: {KEY_FILE_PATH}")
    try:
        with open(KEY_FILE_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
            azure_conf = config.get("azure", {})
            return azure_conf.get("endpoint"), azure_conf.get("key")
    except Exception as e:
        raise RuntimeError(f"設定ファイルの読み込みに失敗しました: {e}")

def clean_japanese_text(text):
    """
    日本語テキストのクリーニング関数：
    PDFのレイアウト解析によって生じた、本来繋がっているはずの文中の不要な改行を除去します。
    
    例: "政権交\n代" -> "政権交代"
    
    :param text: クリーニング前のテキスト
    :return: クリーニング後のテキスト
    """
    if not text:
        return ""

    # 1. 日本語文字（漢字・ひらがな・カタカナ・長音）同士の間の改行を除去
    # パターン: [日本語] + 改行(\n) + [日本語]
    pattern_jp = r'([ぁ-んァ-ン一-龥ー])\n([ぁ-んァ-ン一-龥ー])'
    text = re.sub(pattern_jp, r'\1\2', text)
    
    # 2. 英数字と日本語の間の改行も除去（文脈によるが、結合した方が検索に有利な場合が多い）
    # 例: "FCEV\n販売" -> "FCEV販売"
    pattern_mix1 = r'([a-zA-Z0-9])\n([ぁ-んァ-ン一-龥ー])'
    text = re.sub(pattern_mix1, r'\1\2', text)
    
    pattern_mix2 = r'([ぁ-んァ-ン一-龥ー])\n([a-zA-Z0-9])'
    text = re.sub(pattern_mix2, r'\1\2', text)

    return text

def split_pdf(file_path, chunk_size=2, max_pages=None):
    """
    PDFを指定ページ数ごとに分割し、一時ファイルのパスとページオフセットのリストを返す。
    例: [(path_to_p1-2, 0), (path_to_p3-4, 2), ...]
    """
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
        
    reader = PdfReader(file_path)
    total_pages = len(reader.pages)
    
    # 処理上限の設定
    limit = total_pages
    if max_pages is not None:
        limit = min(total_pages, max_pages)
        
    split_info = []
    base_filename = os.path.splitext(os.path.basename(file_path))[0]
    
    for i in range(0, limit, chunk_size):
        writer = PdfWriter()
        # チャンク内のページを追加（例: 0, 1 ページ目）
        end_page = min(i + chunk_size, limit)
        for page_idx in range(i, end_page):
            writer.add_page(reader.pages[page_idx])
            
        temp_filename = f"{base_filename}_part_{i+1}-{end_page}.pdf"
        temp_path = os.path.join(TEMP_DIR, temp_filename)
        
        with open(temp_path, "wb") as f:
            writer.write(f)
            
        split_info.append({
            "path": temp_path,
            "page_offset": i # このチャンクが本来の何ページ目から始まるか
        })
        
    logging.info(f"📄 PDF分割完了: {len(split_info)} 個のパートに分割しました（最大 {limit} ページまで）。")
    return split_info

def analyze_single_part(file_path, client):
    """
    分割された小さなPDF（2ページ）をAzureに送信して解析する
    """
    with open(file_path, "rb") as f:
        poller = client.begin_analyze_document(
            "prebuilt-layout", 
            body=f,
            content_type="application/pdf",
            output_content_format="markdown"
        )
    return poller.result()

def process_and_merge_results(file_name, split_info_list, client):
    """
    分割ファイルを順次処理し、結果を結合するメインロジック
    """
    full_markdown = ""
    full_chunks = []
    
    # コンテキスト引継ぎ用変数
    last_section_title = "章題なし/導入部"
    
    total_parts = len(split_info_list)
    
    for idx, info in enumerate(split_info_list):
        path = info["path"]
        offset = info["page_offset"] # ページ番号の補正値（例: 2）
        
        logging.info(f"🔄 処理中 ({idx+1}/{total_parts}): {os.path.basename(path)} (Offset: {offset})...")
        
        try:
            # 1. Azure解析実行
            result = analyze_single_part(path, client)
            
            # 2. Markdownの結合（ここでテキストクリーニングを適用）
            # Markdown全体に対してもクリーニングを行うことで、可読性を向上させる
            cleaned_markdown_content = clean_japanese_text(result.content)
            
            full_markdown += f"\n\n<!-- Split Part {idx+1} (Pages {offset+1}~) -->\n\n"
            full_markdown += cleaned_markdown_content
            
            # 3. チャンク抽出とメタデータ補正
            part_chunks, last_section_title = extract_chunks_with_offset(
                result, 
                file_name, 
                offset, 
                last_section_title # 前のパートの最後の章タイトルを渡す
            )
            full_chunks.extend(part_chunks)
            
            # APIレート制限への配慮（念のため1秒待機）
            time.sleep(1)
            
        except Exception as e:
            logging.error(f"❌ パート処理失敗: {path} - {e}")
            # エラーが出ても他のパートの処理は続行するか、ここで中断するか
            # ここではログを出して続行します
            
    return full_markdown, full_chunks

def extract_chunks_with_offset(result: AnalyzeResult, file_name: str, page_offset: int, initial_section_title: str):
    """
    解析結果からチャンクを抽出し、ページ番号を正しいもの（offset加算）に修正する。
    """
    chunks = []
    current_section_title = initial_section_title
    current_text_buffer = []
    current_page_nums = set()
    
    if result.paragraphs:
        for para in result.paragraphs:
            role = para.role if hasattr(para, 'role') else None
            
            # ★ここでテキストクリーニングを適用★
            # JSONデータ（ベクトル検索用）の改行を除去する
            content = clean_japanese_text(para.content)
            
            # ★重要: Azureが返すページ番号(1始まり)にオフセットを加算して、本来のページ番号に戻す
            raw_page_num = para.bounding_regions[0].page_number if para.bounding_regions else 1
            real_page_num = raw_page_num + page_offset
            
            # ノイズ除去
            if role in ["pageHeader", "pageFooter"]:
                continue
                
            # セクション見出しの検出
            if role == "sectionHeading":
                if current_text_buffer:
                    chunks.append({
                        "text": "\n".join(current_text_buffer),
                        "metadata": {
                            "file_name": file_name,
                            "page_numbers": list(current_page_nums),
                            "section_title": current_section_title, # ここには前のセクション名が入る
                            "type": "text_block"
                        }
                    })
                    current_text_buffer = []
                    current_page_nums = set()
                
                current_section_title = content
                current_text_buffer.append(f"【セクション：{content}】")
                current_page_nums.add(real_page_num)
                
            else:
                current_text_buffer.append(content)
                current_page_nums.add(real_page_num)

        # 残りのバッファ処理
        if current_text_buffer:
            chunks.append({
                "text": "\n".join(current_text_buffer),
                "metadata": {
                    "file_name": file_name,
                    "page_numbers": list(current_page_nums),
                    "section_title": current_section_title,
                    "type": "text_block"
                }
            })

    # 次のパートのために、最後のセクションタイトルを返す
    return chunks, current_section_title

def main():
    # 設定読み込み
    try:
        endpoint, key = load_azure_config()
        client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    except Exception as e:
        logging.error(f"初期化エラー: {e}")
        return

    # ディレクトリ準備
    if not os.path.exists(INPUT_DIR):
        os.makedirs(INPUT_DIR)
        return
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    pdf_files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith(".pdf")]
    
    if not pdf_files:
        logging.warning("PDFファイルが見つかりません。")
        return

    logging.info(f"処理開始: 対象 {len(pdf_files)} ファイル (各ファイル最大 {TARGET_MAX_PAGES} ページまで処理)")

    for filename in pdf_files:
        file_path = os.path.join(INPUT_DIR, filename)
        base_name = os.path.splitext(filename)[0]
        
        try:
            # 1. 分割 (Splitting)
            # Free Tier用に chunk_size=2 に固定
            split_info_list = split_pdf(file_path, chunk_size=2, max_pages=TARGET_MAX_PAGES)
            
            if not split_info_list:
                logging.warning(f"スキップ: {filename} の分割に失敗、またはページがありません。")
                continue

            # 2. 順次解析と結合 (Processing & Merging)
            merged_markdown, merged_chunks = process_and_merge_results(filename, split_info_list, client)
            
            # 3. 結果保存
            # Markdown
            md_path = os.path.join(OUTPUT_DIR, f"{base_name}_full.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(merged_markdown)
            
            # JSON Chunks
            json_path = os.path.join(OUTPUT_DIR, f"{base_name}_chunks.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(merged_chunks, f, ensure_ascii=False, indent=2)
                
            logging.info(f"✅ 全工程完了: {filename}")
            logging.info(f"   -> 合計チャンク数: {len(merged_chunks)}")
            logging.info(f"   -> 出力先: {json_path}")

        except Exception as e:
            logging.error(f"❌ 全体処理失敗: {filename} - {str(e)}")
            
        finally:
            # 一時ファイルの削除（クリーンアップ）
            if os.path.exists(TEMP_DIR):
                shutil.rmtree(TEMP_DIR)
                logging.info("🧹 一時ファイルを削除しました。")

if __name__ == "__main__":
    main()