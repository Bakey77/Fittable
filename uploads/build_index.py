import os
import sys
import re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llama_index.core import Document
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.core import StorageContext
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from backend.services.llm import get_embedding_model
from config import Config

QDRANT_HOST = "127.0.0.1"                                 
QDRANT_PORT = 6333

os.environ['HTTPX_HTTP2'] = 'false'


# Qdrant 配置
COLLECTION_NAME = "fitness_guide"

# 文档路径
# DOC_PATH = "fitness-form-guide.md"
DOC_PATH = "/Users/liaomeiqi/Downloads/Fit-Agent/uploads/fitness-form-guide.md"

# 肌肉群映射
MUSCLE_GROUPS = {
    "深蹲": ["下肢", "臀部", "股四头肌"],
    "硬拉": ["臀部", "股后侧", "下背"],
    "俯卧撑": ["胸", "肩", "肱三头肌"],
    "平板支撑": ["核心", "腹横肌"],
    "俯身划船": ["背部", "后肩", "肱二头肌"],
    "过顶推举": ["肩", "三角肌"],
    "腿举": ["股四头肌", "臀部"],
    "腿屈伸": ["股四头肌"],
    "腿弯举": ["股后侧"],
    "高位下拉": ["背阔肌", "上背"],
    "划船机": ["中背部", "背阔肌"],
    "推胸机": ["胸", "前三角", "肱三头肌"],
    "蝴蝶机": ["胸"],
    "肩推机": ["三角肌"],
    "绳索下压": ["肱三头肌"],
    "弯举机": ["肱二头肌"],
    "提踵": ["小腿"],
    "髋外展": ["臀中肌", "臀小肌"],
    "髋内收": ["大腿内侧"],
    "哈克深蹲": ["股四头肌", "臀部"],
    "引体向上": ["背阔肌", "上背", "肱二头肌"],
    "臂屈伸": ["胸", "肱三头肌", "肩"],
    "反向飞鸟": ["后三角", "菱形肌"],
    "面拉": ["后三角", "外旋肌"],
    "侧平举": ["三角肌中束"],
    "后踢": ["臀大肌"],
    "臀推": ["臀大肌"],
    "卷腹": ["腹直肌"],
}


def parse_exercise_content(exercise_name: str, content: str) -> dict:
    """解析动作内容，提取各部分"""
    result = {
        "goal": "",
        "instructions": "",
        "mistakes": ""
    }

    if "**适合目标**" in content:
        parts = content.split("**适合目标**")
        if len(parts) > 1:
            goal_section = parts[1]
            if "**动作要领**" in goal_section:
                result["goal"] = goal_section.split("**动作要领**")[0].strip()
            else:
                result["goal"] = goal_section.strip()

    if "**动作要领**" in content:
        parts = content.split("**动作要领**")
        if len(parts) > 1:
            instr_section = parts[1]
            if "**常见错误**" in instr_section:
                result["instructions"] = instr_section.split("**常见错误**")[0].strip()
            else:
                result["instructions"] = instr_section.strip()

    if "**常见错误**" in content:
        parts = content.split("**常见错误**")
        if len(parts) > 1:
            result["mistakes"] = parts[1].strip()

    return result


def load_documents_by_exercise(file_path: str) -> list[Document]:
    """按动作分割文档，每个动作作为一个语义 chunk"""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    documents = []

    # 提取通用原则（作为单独文档）
    通用原则_match = re.search(r"## 一、通用动作原则\s*\n(.*?)(?=## 二、|$)", content, re.DOTALL)
    if 通用原则_match:
        通用原则 = 通用原则_match.group(1).strip()
        doc = Document(
            text=f"# 通用动作原则\n{通用原则}",
            metadata={
                "source": file_path,
                "type": "通用原则",
                "title": "通用动作原则"
            }
        )
        documents.append(doc)

    # 提取基础动作
    基础动作_match = re.search(r"## 二、基础动作要领\s*\n(.*?)(?=## 三、|$)", content, re.DOTALL)
    if 基础动作_match:
        section = 基础动作_match.group(1)
        # 提取所有动作：标题+内容
        exercise_pattern = re.compile(r"### \d+\. ([^\n]+)\n(.*?)(?=\n### \d+\. |$)", re.DOTALL)
        for match in exercise_pattern.finditer(section):
            exercise_name = match.group(1).strip()
            exercise_content = match.group(2).strip()

            parsed = parse_exercise_content(exercise_name, exercise_content)

            # 组合完整文本
            full_text = f"## {exercise_name}\n\n**适合目标**\n{parsed['goal']}\n\n**动作要领**\n{parsed['instructions']}\n\n**常见错误**\n{parsed['mistakes']}"

            doc = Document(
                text=full_text,
                metadata={
                    "source": file_path,
                    "type": "基础动作",
                    "title": exercise_name,
                    "muscle_groups": MUSCLE_GROUPS.get(exercise_name, [])
                }
            )
            documents.append(doc)

    # 提取器械动作
    器械动作_match = re.search(r"## 三、常见器械动作要领\s*\n(.*?)(?=## 四、|$)", content, re.DOTALL)
    if 器械动作_match:
        section = 器械动作_match.group(1)
        exercise_pattern = re.compile(r"### \d+\. ([^\n]+)\n(.*?)(?=\n### \d+\. |$)", re.DOTALL)
        for match in exercise_pattern.finditer(section):
            exercise_name = match.group(1).strip()
            exercise_content = match.group(2).strip()

            parsed = parse_exercise_content(exercise_name, exercise_content)

            full_text = f"## {exercise_name}\n\n**适合目标**\n{parsed['goal']}\n\n**动作要领**\n{parsed['instructions']}\n\n**常见错误**\n{parsed['mistakes']}"

            doc = Document(
                text=full_text,
                metadata={
                    "source": file_path,
                    "type": "器械动作",
                    "title": exercise_name,
                    "muscle_groups": MUSCLE_GROUPS.get(exercise_name, [])
                }
            )
            documents.append(doc)

    # 提取实用建议
    实用建议_match = re.search(r"## 四、器械训练时的实用建议\s*\n(.*?)$", content, re.DOTALL)
    if 实用建议_match:
        实用建议 = 实用建议_match.group(1).strip()
        doc = Document(
            text=f"# 器械训练实用建议\n{实用建议}",
            metadata={
                "source": file_path,
                "type": "实用建议",
                "title": "器械训练时的实用建议"
            }
        )
        documents.append(doc)

    return documents


def create_qdrant_collection(client: QdrantClient, collection_name: str, vector_size: int = 1536):
    """创建Qdrant collection（如果不存在）"""
    collections = client.get_collections().collections
    collection_names = [c.name for c in collections]

    if collection_name not in collection_names:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE)
        )
        print(f"Created collection: {collection_name}")
    else:
        print(f"Collection already exists: {collection_name}")


def build_index():
    """构建向量索引并存储到Qdrant"""
    # 初始化Qdrant客户端
    client = QdrantClient(host=Config.QDRANT_HOST, port=Config.QDRANT_PORT)
    
    # 创建collection
    create_qdrant_collection(client, COLLECTION_NAME)

    # 初始化嵌入模型
    embed_model = get_embedding_model()

    # 初始化Qdrant向量存储
    vector_store = QdrantVectorStore(
        client=client,
        collection_name=COLLECTION_NAME,
        embedding=embed_model
    )

    # 创建存储上下文
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    # 加载文档（按动作分割）
    docs = load_documents_by_exercise(DOC_PATH)
    print(f"Loaded {len(docs)} exercise documents from {DOC_PATH}")

    # 直接使用 Document 列表创建索引（LlamaIndex 会自动处理嵌入）
    from llama_index.core import VectorStoreIndex
    index = VectorStoreIndex.from_documents(
        docs,
        storage_context=storage_context,
        embed_model=embed_model
    )

    print(f"Index built successfully! Total documents: {len(docs)}")

    return index


if __name__ == "__main__":
    build_index()
