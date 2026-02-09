import os
import sys
from typing import Any, List, Optional, Mapping
from openai import OpenAI
import time
import random

# LangChainの基本クラスをインポート
from langchain_core.language_models.llms import LLM
from langchain_core.callbacks.manager import CallbackManagerForLLMRun

# プロジェクトルートの設定（playwright_testからAPIキーを読み込む）
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from playwright_test import (
        MODEL_SCOPE_API_KEY, 
        MODEL_SCOPE_BASE_URL, 
        MODEL_SCOPE_MODEL_ID
    )
except ImportError:
    # インポートに失敗した場合のフォールバック（環境変数またはハードコーディング）
    MODEL_SCOPE_API_KEY = os.getenv("MODEL_SCOPE_API_KEY")
    MODEL_SCOPE_BASE_URL = "https://api-inference.modelscope.cn/v1"
    MODEL_SCOPE_MODEL_ID = "Qwen/Qwen2.5-32B-Instruct"

class ModelScopeLLM(LLM):
    """
    RAGAS評価フレームワークでModelScope（Qwen）を使用するためのカスタムLangChainラッパーです。
    コスト削減のため、OpenAI（GPT-4）の代わりに使用します。
    """
    
    model_name: str = MODEL_SCOPE_MODEL_ID
    api_key: str = MODEL_SCOPE_API_KEY
    base_url: str = MODEL_SCOPE_BASE_URL
    client: Any = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # OpenAI互換クライアントの初期化
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
        )

    @property
    def _llm_type(self) -> str:
        return "modelscope_qwen"

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        
        # 最大リトライ回数
        max_retries = 5
        
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": "You are a helpful and fair assistant for evaluating RAG systems."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.0,
                    stream=False,
                    # Error 400対策 (enable_thinkingを無効化)
                    extra_body={"enable_thinking": False} 
                )
                return response.choices[0].message.content
                
            except Exception as e:
                error_str = str(e)
                # レート制限(429)の場合のみ待機してリトライ
                if "429" in error_str or "Too Many Requests" in error_str:
                    wait_time = (2 ** attempt) + random.uniform(0, 1) # 指数バックオフ
                    print(f"⚠️ Rate limit hit. Retrying in {wait_time:.2f}s...")
                    time.sleep(wait_time)
                else:
                    # その他のエラーはログを出して再試行（または停止）
                    print(f"ModelScope API エラー: {e}")
                    # 400エラー等の場合はリトライしても無駄なのでループを抜けるべきだが、
                    # とりあえず短い待機を入れておく
                    time.sleep(1)

        return "Error: Unable to evaluate"

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        return {"model_name": self.model_name}