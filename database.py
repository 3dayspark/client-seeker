import logging
import os
import json
import asyncio
import re
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text, inspect


load_dotenv()
logger = logging.getLogger(__name__)


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


# rag_utils が存在しない場合でもエラーにならないように try-except で囲む
try:
    from rag_utils import Settings
    # モデルが未ロードならここでロード（rag_utilsの実装に依存）
    if Settings.embed_model is None:
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-zh-v1.5")
    logger.info("✅ Embedding model loaded from rag_utils.")
except ImportError:
    logger.warning("⚠️ rag_utils not found. Semantic search will return zero vectors.")
    Settings = None


# { "table_name": {"pk": "id", "columns": ["name", "industry"...]} }
VECTOR_TABLE_CACHE: Dict[str, Dict[str, Any]] = {}


IGNORED_COLUMNS = {
    'search_embedding', 'id', 'created_at', 'updated_at', 
    'password', 'embedding', 'vector'
}



async def get_database_schema_info(refresh=False):
    """
    データベースのスキーマ情報（テーブル名、カラム名、型、サンプル値）を取得します。
    """
    if not refresh and os.path.exists(SCHEMA_CACHE_FILE):
        logger.info("📂 DBスキーマ情報をキャッシュから読み込み中...")
        with open(SCHEMA_CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read()

    logger.info("🔄 DBスキーマ情報をデータベースから抽出中...")
    
    async with engine.connect() as conn:
        def _inspect_schema(connection):
            inspector = inspect(connection)
            table_names = inspector.get_table_names()
            
            info_list = []
            for table in table_names:
                columns = inspector.get_columns(table)
                col_defs = []
                for col in columns:
                    col_defs.append(f"  {col['name']} {str(col['type'])}")
                
                # LLMが見やすい CREATE TABLE 形式
                table_desc = f"CREATE TABLE {table} (\n" + ",\n".join(col_defs) + "\n);"
                
                try:
                    sample_query = text(f"SELECT * FROM {table} LIMIT 3")
                    samples = connection.execute(sample_query).fetchall()
                    sample_str = str([tuple(row) for row in samples])
                    table_desc += f"\n-- Sample Rows: {sample_str}\n"
                except Exception:
                    pass
                
                info_list.append(table_desc)
            return "\n\n".join(info_list)

        full_description = await conn.run_sync(_inspect_schema)

    with open(SCHEMA_CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(full_description)
    
    logger.info("✅ DBスキーマ情報の抽出とキャッシュが完了しました。")
    return full_description


def get_table_schema_sync(table_name: str):
    """特定テーブルのスキーマ定義だけを同期的に取得する（エラーハンドリング用）"""
    if os.path.exists(SCHEMA_CACHE_FILE):
        with open(SCHEMA_CACHE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
            pattern = re.compile(f"CREATE TABLE {table_name} .*?;", re.DOTALL)
            match = pattern.search(content)
            if match:
                return match.group(0)
    return f"Schema info for {table_name} not found."


async def execute_raw_sql(sql_query: str):
    """
    LLMが生成した生のSQLを実行します。読み取り専用トランザクションを強制。
    """
    logger.info(f"⚡ SQL実行: {sql_query}")
    
    forbidden_keywords = ["DROP ", "DELETE ", "UPDATE ", "INSERT ", "ALTER ", "TRUNCATE ", "GRANT ", "REVOKE "]
    upper_sql = sql_query.upper()
    if any(k in upper_sql for k in forbidden_keywords):
        return {"error": "Security Alert: データ変更操作は許可されていません。SELECTのみを使用してください。"}

    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
            stmt = text(sql_query)
            result = await session.execute(stmt)
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


async def search_column_values(table_name: str, column_name: str, keyword: str = None, limit: int = 10):
    """
    指定されたテーブルのカラム内に、どのような値が存在するかを確認します。
    """
    logger.info(f"🔎 Inspecting values: {table_name}.{column_name} (kw={keyword})")
    
    async with AsyncSessionLocal() as session:
        try:
            # 簡易セキュリティチェック
            if not (table_name.replace("_","").isalnum() and column_name.replace("_","").isalnum()):
                return {"error": "Invalid table or column name."}

            if keyword:
                stmt = text(f"SELECT DISTINCT {column_name} FROM {table_name} WHERE {column_name}::text ILIKE :kw LIMIT :lim")
                result = await session.execute(stmt, {"kw": f"%{keyword}%", "lim": limit})
            else:
                stmt = text(f"SELECT {column_name}, COUNT(*) as cnt FROM {table_name} GROUP BY {column_name} ORDER BY cnt DESC LIMIT :lim")
                result = await session.execute(stmt, {"lim": limit})

            rows = [row[0] for row in result.fetchall()]
            return {"table": table_name, "column": column_name, "found_values": rows}
        except Exception as e:
            logger.error(f"Value Inspection Error: {e}")
            return {"error": str(e)}




async def refresh_vector_schema_cache():
    """
    DBをスキャンし、「search_embedding」カラムを持つすべてのテーブル定義を特定・キャッシュします。
    """
    global VECTOR_TABLE_CACHE
    VECTOR_TABLE_CACHE = {}
    
    logger.info("🕵️ Scanning database schema for vector-enabled tables...")
    
    async with engine.connect() as conn:
        def _inspect(connection):
            insp = inspect(connection)
            tables = insp.get_table_names()
            valid_tables = {}
            
            for table in tables:
                columns = insp.get_columns(table)
                col_names = [c['name'] for c in columns]
                
                if 'search_embedding' in col_names:
                    pk_const = insp.get_pk_constraint(table)
                    pk_name = pk_const['constrained_columns'][0] if pk_const['constrained_columns'] else 'id'
                    
                    target_cols = [
                        c['name'] for c in columns 
                        if c['name'] not in IGNORED_COLUMNS 
                        and c['name'] != pk_name
                    ]
                    
                    valid_tables[table] = {
                        "pk": pk_name,
                        "columns": target_cols
                    }
            return valid_tables

        VECTOR_TABLE_CACHE = await conn.run_sync(_inspect)
    
    logger.info(f"✅ Found {len(VECTOR_TABLE_CACHE)} vector-ready tables: {list(VECTOR_TABLE_CACHE.keys())}")


async def get_embedding(text_input: str) -> List[float]:
    """Helper: テキストをベクトル化"""
    if not text_input or not Settings:
        return [0.0] * 1024 
    try:
        # 1024次元 (BGE-M3)
        return await asyncio.to_thread(Settings.embed_model.get_text_embedding, text_input)
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        return [0.0] * 1024


async def vectorize_database():
    """
    キャッシュされたスキーマに基づき、全テーブルの未計算レコードを自動的にベクトル化します。
    """
    if not VECTOR_TABLE_CACHE:
        await refresh_vector_schema_cache()

    logger.info("🔄 Starting generic database vectorization...")

    async with AsyncSessionLocal() as session:
        for table_name, schema in VECTOR_TABLE_CACHE.items():
            pk = schema['pk']
            cols = schema['columns']
            
            select_cols = ", ".join([pk] + cols)
            query = f"SELECT {select_cols} FROM {table_name} WHERE search_embedding IS NULL"
            
            result = await session.execute(text(query))
            rows = result.fetchall()
            
            if not rows:
                continue

            logger.info(f"Processing {len(rows)} new rows in '{table_name}'...")

            for row in rows:
                text_parts = []
                for col in cols:
                    val = getattr(row, col)
                    if val is not None and str(val).strip():
                        text_parts.append(f"{col}:{val}")
                
                combined_text = " ".join(text_parts)
                vec = await get_embedding(combined_text)
                
                update_sql = f"UPDATE {table_name} SET search_embedding = :vec WHERE {pk} = :pk_val"
                await session.execute(text(update_sql), {"vec": str(vec), "pk_val": getattr(row, pk)})
        
        await session.commit()
    logger.info("✅ Generic vectorization complete.")


async def search_table_semantically(table_name: str, query_text: str, limit: int = 5):
    """
    指定されたテーブルに対してベクトル検索を行います。
    """
    if not VECTOR_TABLE_CACHE:
        await refresh_vector_schema_cache()
    
    if table_name not in VECTOR_TABLE_CACHE:
        logger.warning(f"⚠️ Requested semantic search on unknown table: {table_name}")
        return []

    schema = VECTOR_TABLE_CACHE[table_name]
    pk = schema['pk']
    return_cols = ", ".join([pk] + schema['columns'])

    query_vector = await get_embedding(query_text)

    async with AsyncSessionLocal() as session:
        try:
            sql = f"""
                SELECT {return_cols}, 1 - (search_embedding <=> :vec) AS similarity
                FROM {table_name}
                ORDER BY search_embedding <=> :vec
                LIMIT :limit
            """
            
            result = await session.execute(text(sql), {"vec": str(query_vector), "limit": limit})
            rows = [dict(zip(result.keys(), row)) for row in result.fetchall()]
            
            # 閾値フィルタ
            filtered_rows = [r for r in rows if r['similarity'] > 0.4]
            return filtered_rows

        except Exception as e:
            logger.error(f"Generic Vector Search Error on {table_name}: {e}")
            return []