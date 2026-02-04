import logging
import os
import json
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text, inspect

load_dotenv()
logger = logging.getLogger(__name__)

# --- DB接続設定 ---
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# キャッシュファイルのパス
SCHEMA_CACHE_FILE = "db_schema_cache.txt"

async def get_database_schema_info(refresh=False):
    """
    データベースのスキーマ情報（テーブル名、カラム名、型、サンプル値）を取得します。
    RAGのインデックスのように、初回はDBから読み取りファイルにキャッシュします。
    """
    # キャッシュが存在し、refreshフラグがなければキャッシュを返す
    if not refresh and os.path.exists(SCHEMA_CACHE_FILE):
        logger.info("📂 DBスキーマ情報をキャッシュから読み込み中...")
        with open(SCHEMA_CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read()

    logger.info("🔄 DBスキーマ情報をデータベースから抽出中...")
    
    schema_desc = []
    
    async with engine.connect() as conn:
        # 非同期エンジンでSynchronousなinspectを使うためのrun_sync
        def _inspect_schema(connection):
            inspector = inspect(connection)
            table_names = inspector.get_table_names()
            
            info_list = []
            for table in table_names:
                columns = inspector.get_columns(table)
                col_info = []
                
                # 各カラムの情報を取得
                for col in columns:
                    col_str = f"{col['name']} ({str(col['type'])})"
                    col_info.append(col_str)
                
                # サンプルデータを3件取得して、値の傾向をLLMに伝える
                # 注意: TEXT/VARCHAR型のカラムのみサンプルを取得するなどの工夫も有効
                try:
                    sample_query = text(f"SELECT * FROM {table} LIMIT 3")
                    samples = connection.execute(sample_query).fetchall()
                    sample_str = str([tuple(row) for row in samples])
                except Exception:
                    sample_str = "No samples"

                table_desc = (
                    f"Table Name: {table}\n"
                    f"Columns: {', '.join(col_info)}\n"
                    f"Sample Data (Limit 3): {sample_str}\n"
                )
                info_list.append(table_desc)
            return "\n".join(info_list)

        full_description = await conn.run_sync(_inspect_schema)

    # キャッシュに保存
    with open(SCHEMA_CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(full_description)
    
    logger.info("✅ DBスキーマ情報の抽出とキャッシュが完了しました。")
    return full_description


async def execute_raw_sql(sql_query: str):
    """
    LLMが生成した生のSQLを実行します。
    セキュリティ対策: 読み取り専用トランザクションを強制。
    """
    logger.info(f"⚡ SQL実行: {sql_query}")
    
    # 1. キーワードによる簡易フィルター (大文字小文字無視)
    # これらはREAD ONLYモードでもエラーになるが、早めに弾くために残す
    forbidden_keywords = ["DROP ", "DELETE ", "UPDATE ", "INSERT ", "ALTER ", "TRUNCATE ", "GRANT ", "REVOKE "]
    upper_sql = sql_query.upper()
    if any(k in upper_sql for k in forbidden_keywords):
        logger.warning(f"⚠️ Security Alert: Forbidden keyword detected in {sql_query}")
        return {"error": "Security Alert: データ変更操作は許可されていません。SELECTのみを使用してください。"}

    async with AsyncSessionLocal() as session:
        try:
            # 2. 【重要】セッションを読み取り専用モードに設定
            # これにより、このトランザクション内で書き込みが発生するとDB側でエラーになる
            await session.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
            
            stmt = text(sql_query)
            result = await session.execute(stmt)
            
            # 結果を辞書リスト形式に変換
            keys = result.keys()
            rows = result.fetchall()
            
            data = [dict(zip(keys, row)) for row in rows]
            
            # 日付型などを文字列に変換
            for item in data:
                for k, v in item.items():
                    if hasattr(v, 'isoformat'):
                        item[k] = v.isoformat()
            
            return data

        except Exception as e:
            error_msg = str(e).split('\n')[0]
            logger.error(f"❌ SQL Execution Error: {error_msg}")
            return {"error": f"SQL Execution Failed: {error_msg}"}