"""问答相关 schema。"""
from typing import List, Literal, Optional

from pydantic import BaseModel


class QaAnswerRequest(BaseModel):
    query: str
    modelType: Optional[str] = None       # deepseek | qwen | doubao
    conversationId: Optional[str] = None  # 多轮对话 id（首次不传则新建）
    agentMode: bool = False               # S2：深度思考(Agent)模式，走通用 Agent 引擎
    memoryRead: bool = False              # Agent 模式长期记忆读取 opt-in（默认关=现状）
    memoryWrite: bool = False             # Agent 模式长期记忆写入 opt-in（默认关=现状）
    memoryScope: Literal["user", "device"] = "user"  # 记忆归属域（tenant 域禁止：跨用户记忆=越权）


class QaAnswerData(BaseModel):
    answer: str
    retrievalSource: List[dict] = []
    responseTime: float = 0.0
    hallucinationRate: float = 0.0
    cached: bool = False
    conversationId: str = ""


class TermRequest(BaseModel):
    term: str


class FeedbackRequest(BaseModel):
    query: str
    answer: str
    feedback: str            # like | dislike
    conversationId: Optional[str] = None
    reason: Optional[str] = None          # 用户纠错理由/标注（沉淀坏 case）
    retrievalSources: Optional[str] = None  # 检索命中的文档名（逗号分隔，旧客户端兼容）
    traceId: Optional[str] = None         # 关联问答链路 trace（非法值自动重建）
    sources: Optional[List[dict]] = None  # 结构化检索来源（优先于 retrievalSources）


class FaithfulnessRequest(BaseModel):
    answer: str
    sources: List[dict] = []              # 引用来源（[{text,...}]），LLM-judge 判定支撑率
    modelType: Optional[str] = None


class RenameRequest(BaseModel):
    title: str


class RelatedRequest(BaseModel):
    query: str
    answer: str = ""
    modelType: Optional[str] = None       # deepseek | qwen | doubao


class ExportRequest(BaseModel):
    query: str
    answer: str
    sources: List[dict] = []
    meta: Optional[dict] = None           # confidence/hallucinationRate/responseTime


class BatchDeleteRequest(BaseModel):
    ids: List[str]
