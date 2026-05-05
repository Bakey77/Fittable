import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llama_index.core import Document
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.core import StorageContext
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from backend.services.llm import get_embedding_model
from config import Config


# Qdrant 配置
COLLECTION_NAME = "recipes"

# 文档路径
DOC_PATH = "fitness_recipes.md"


def load_document(file_path: str) -> list[Document]:
    """加载markdown文档，按食谱分割成多个Document"""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 按 ## 分割每个食谱
    sections = content.split("## ")
    documents = []

    for section in sections[1:]:  # 跳过标题行
        lines = section.split("\n", 1)
        if len(lines) < 2:
            continue

        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""

        # 提取营养成分
        calories = protein = fat = carbs = 0
        for line in body.split("\n"):
            if "热量" in line:
                import re
                cal_match = re.search(r"热量\s*(\d+)", line)
                pro_match = re.search(r"蛋白质\s*(\d+)", line)
                fat_match = re.search(r"脂肪\s*(\d+)", line)
                carb_match = re.search(r"碳水化合物\s*(\d+)", line)
                if cal_match: calories = int(cal_match.group(1))
                if pro_match: protein = int(pro_match.group(1))
                if fat_match: fat = int(fat_match.group(1))
                if carb_match: carbs = int(carb_match.group(1))

        doc = Document(
            text=f"## {title}\n\n{body}",
            metadata={
                "source": file_path,
                "title": title,
                "calories": calories,
                "protein": protein,
                "fat": fat,
                "carbs": carbs
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


def build_recipes_index():
    """构建食谱向量索引并存储到Qdrant"""
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

    # 加载文档（按食谱分割）
    documents = load_document(DOC_PATH)
    print(f"Loaded {len(documents)} recipes from {DOC_PATH}")

    # 存入向量数据库
    from llama_index.core import VectorStoreIndex
    from llama_index.core.node_parser import MarkdownNodeParser

    # 将文档转换为节点
    parser = MarkdownNodeParser()
    nodes = parser.get_nodes_from_documents(documents)

    index = VectorStoreIndex(
        nodes=nodes,
        storage_context=storage_context,
        embed_model=embed_model
    )

    print(f"Index built successfully! Total recipes: {len(documents)}")

    return index


if __name__ == "__main__":
    build_recipes_index()
