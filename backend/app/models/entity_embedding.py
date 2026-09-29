"""实体名向量（设计文档 §16.7；实体链接的向量召回用）。

主键用实体名的 sha1：MySQL 默认排序规则不区分大小写，Neo4j 区分，以名字作主键会让
"iPhone"/"iphone" 这类实体冲突。换 embedding 模型时按 model 列整表重算。
"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EntityEmbedding(Base):
    __tablename__ = "entity_embedding"

    name_hash: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    vector: Mapped[str] = mapped_column(Text, nullable=False)          # float32 数组的 base64
    model: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
