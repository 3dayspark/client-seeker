import json
import time
from bs4 import BeautifulSoup
from openai import OpenAI  # ModelScope 兼容 OpenAI SDK
from playwright.sync_api import sync_playwright

# =================配置区域=================
# 替换为你的 ModelScope API Key
MODELSCOPE_API_KEY = "ms-53b1ff87-bf6d-4b4d-8237-886fa8eb7064"
MODEL_NAME = "Qwen/Qwen2.5-32B-Instruct" # ModelScope当前API支持的标准模型名称，若Qwen3未上线API可用Qwen2.5代替，逻辑通用
TARGET_URL = "https://www.qcc.com/web/search/advance"

# 假设这是用户输入的自然语言需求
USER_QUERY = "我想找一下北京和上海的，成立时间在1到5年之间的，注册资本在500万到1000万的小型科技类公司，且状态是正常的。"
# =========================================

class QccFormAgent:
    def __init__(self, html_content):
        self.html = html_content
        self.schema = {} # 存储解析后的表单结构

    def parse_html_to_schema(self):
        """
        步骤1：解析HTML，提取所有筛选维度和对应的选项
        """
        soup = BeautifulSoup(self.html, 'html.parser')
        panels = soup.find_all(class_='advance-filters-panel')
        
        form_structure = []

        for panel in panels:
            # 1. 获取板块标题 (例如：所属行业、省份地区、企业规模)
            title_tag = panel.find(class_='title')
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            
            field_info = {"field_name": title, "type": "unknown", "options": []}

            # 2. 提取具体的选项 (Checkbox/Radio)
            # 企查查的选项通常在 checkbox-element 或 radio-element 中
            items = panel.find_all(class_=['checkbox-element', 'radio-element', 'adv-common-select'])
            
            options = []
            for item in items:
                # 尝试获取显示的文本
                text_div = item.find(class_='element-title')
                if text_div:
                    opt_text = text_div.get_text(strip=True)
                    options.append(opt_text)
                
                # 处理下拉框类型的（如省份），这里简化处理，提示LLM这是一个需要填写的字段或特定列表
                if "select" in item.get('class', []):
                    # 检查是否有特定的下拉触发器文字
                    trigger = item.find(class_='qccd-dropdown-trigger')
                    if trigger:
                        options.append(f"Dropdown: {trigger.get_text(strip=True)}")

            # 3. 提取输入框 (例如：注册资本的区间输入)
            inputs = panel.find_all('input', type='text')
            if inputs and not options:
                field_info["type"] = "input_text"
                field_info["description"] = "需要输入具体文本或数值范围"
            elif options:
                field_info["type"] = "selection"
                field_info["options"] = options
            
            form_structure.append(field_info)
        
        self.schema = form_structure
        return form_structure

    def get_ai_decision(self, user_query):
        """
        步骤2：调用 ModelScope Qwen 模型进行决策
        """
        print("正在请求 ModelScope 模型进行决策...")
        
        # 构建提示词
        system_prompt = f"""
        你是一个浏览器自动化助手。你的任务是根据用户的自然语言描述，从给定的网页表单选项中选择最匹配的筛选项。
        
        【表单结构定义】：
        {json.dumps(self.schema, ensure_ascii=False, indent=2)}
        
        【输出规则】：
        1. 必须返回纯 JSON 格式。
        2. JSON 的 Key 必须是【表单结构定义】中的 "field_name"。
        3. JSON 的 Value 必须是一个列表，包含需要勾选的选项名称（必须严格匹配 options 中的文本）。
        4. 如果用户提到的条件在表单中没有对应的选项，请忽略该条件。
        5. 如果是输入框类型，Value 填入用户指定的具体值。
        
        例如用户说“找北京的公司”，表单中有“省份地区”包含“北京市”，则输出 {{"省份地区": ["北京市"]}}。
        """

        client = OpenAI(
            api_key=MODELSCOPE_API_KEY,
            base_url="https://api-inference.modelscope.cn/v1"
        )

        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"用户需求：{user_query}"}
                ],
                temperature=0.1, # 降低随机性
                response_format={"type": "json_object"} # 强制 JSON 模式（部分模型支持）
            )
            result_text = response.choices[0].message.content
            # 清理可能存在的 Markdown 标记
            result_text = result_text.replace("```json", "").replace("```", "").strip()
            decision_json = json.loads(result_text)
            print("LLM 决策结果:", json.dumps(decision_json, ensure_ascii=False, indent=2))
            return decision_json
        except Exception as e:
            print(f"ModelScope 调用失败: {e}")
            return {}

def execute_automation(decision_data):
    """
    步骤3：使用 Playwright 执行自动化操作
    注意：企查查有严格的反爬验证（滑块、登录），此代码演示操作逻辑，
    实际运行通常需要先手动登录保存 Cookies 或使用调试端口接管浏览器。
    """
    print("正在启动浏览器执行操作...")
    
    with sync_playwright() as p:
        # 启动浏览器（有头模式，方便观察）
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
        page = context.new_page()
        
        # 打开网页
        page.goto(TARGET_URL)
        page.wait_for_load_state('networkidle')

        # 提示：这里通常需要处理登录，建议休眠几十秒手动扫码登录，或者加载本地Cookies
        print("请在浏览器中完成登录（如果弹出）...")
        # time.sleep(20) # 根据需要开启手动登录等待时间

        # 遍历决策数据进行操作
        for field, targets in decision_data.items():
            print(f"正在处理字段: {field} -> 目标: {targets}")
            
            # 简单策略：根据文本内容点击
            # 这里的逻辑是：找到包含该字段标题的区域，然后在该区域内点击对应的选项
            
            # 1. 定位到对应的大板块
            try:
                # 使用 Playwright 的定位器找到包含特定标题的容器
                # 注意：这是一个通用的定位策略，实际可能需要微调
                panel_loc = page.locator(f".advance-filters-panel:has-text('{field}')")
                
                if isinstance(targets, list):
                    for target_val in targets:
                        # 在板块内寻找包含选项文字的元素并点击
                        # 企查查的选项通常在 label 或 span 中
                        option_loc = panel_loc.locator(f"label:has-text('{target_val}')").first
                        
                        if option_loc.is_visible():
                            option_loc.click()
                            print(f"  已点击: {target_val}")
                            time.sleep(0.5) # 稍微停顿防止操作过快
                        else:
                            # 处理特殊情况：如下拉菜单
                            # 如果是省份/行业，可能需要先点击下拉箭头
                            dropdown = panel_loc.locator(".qccd-dropdown-trigger").first
                            if dropdown.is_visible():
                                dropdown.click()
                                time.sleep(0.5)
                                # 在下拉弹窗中点选
                                page.click(f"text={target_val}")
                                # 点击空白处收起下拉（可选）
                                page.mouse.click(0, 0)
                
                elif isinstance(targets, str):
                    # 处理输入框的情况（如关键词）
                    input_loc = panel_loc.locator("input[type='text']").first
                    if input_loc.is_visible():
                        input_loc.fill(targets)
                        print(f"  已输入: {targets}")

            except Exception as e:
                print(f"  操作失败 {field}: {e}")

        print("所有筛选条件填写完毕。")
        
        # 点击查询按钮 (假设类名为 search-btn)
        try:
            page.click(".search-btn")
            print("已点击查询。")
        except:
            print("未找到查询按钮，请手动点击。")

        # 保持浏览器打开以便查看结果
        input("按回车键关闭浏览器...")
        browser.close()

# =================主程序入口=================
if __name__ == "__main__":
    # 1. 读取HTML文件 (根据你的描述，你有一个附件 qcc_example.html)
    # 在实际运行中，你可以让 Playwright 先去抓取这个页面源码，这里模拟从文件读取
    try:
        # 假设文件名是 'qcc_example.html'，你需要将你的 HTML 内容保存为这个文件
        # 或者你可以直接将 html 字符串赋值给变量
        with open("qcc_example.html", "r", encoding="utf-8") as f:
            html_content = f.read()
            
        # 实例化 Agent
        agent = QccFormAgent(html_content)
        
        # 2. 解析 HTML 生成 Schema
        agent.parse_html_to_schema()
        print("HTML 解析完成，Schema 已生成。")
        
        # 3. LLM 决策
        decision = agent.get_ai_decision(USER_QUERY)
        
        if decision:
            # 4. 执行自动化
            execute_automation(decision)
            
    except FileNotFoundError:
        print("错误：请确保目录下存在 qcc_example.html 文件，并将提供的 HTML 内容存入其中。")