"""Sprint3a CIM 导入服务：解析 CIMXML → 连通图 → 存 KgTriple 三元组（复用知识图谱底座）。

最小切片：连通边存为 (设备A, "电气连接", 设备B) 三元组，进 KG 后 GraphRAG 多跳推理
（kg_service.get_paths）天然可用——故障传播链从"实体模糊匹配"升级为"真实电气连通"。
真实 SCADA/CIM 文件接入时，本服务零改动（只换输入）。
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kg_triple import KgTriple
from app.rag.cim_parser import graph_to_triples, parse_cim_xml


async def import_cim(
    db: AsyncSession, xml_text: str, *, doc_id: str = "cim", doc_name: str = "",
) -> dict:
    """解析 CIMXML 并把连通边存为 KgTriple 三元组。

    Returns: {station, equipmentCount, edgeCount, docId}
    异常由调用方（router）兜底；解析失败 parse_cim_xml 抛 ValueError。
    """
    graph = parse_cim_xml(xml_text)
    triples = graph_to_triples(graph)
    station = doc_name or graph["station"] or doc_id
    for subject, relation, obj in triples:
        db.add(KgTriple(
            subject=subject, relation=relation, object=obj,
            doc_id=doc_id, doc_name=station,
        ))
    await db.commit()
    return {
        "station": station,
        "equipmentCount": len(graph["equipments"]),
        "edgeCount": len(triples),
        "docId": doc_id,
    }
