"""CIM (IEC 61970) RDF/XML 解析器 → 电气连通图（设备节点 + ConnectivityNode 连接边）。

Sprint3a 最小切片：把 CIMXML 解析成设备图，供 GraphRAG / 数字孪生消费。
真实 CIM/CIMD 导出文件直接喂入；无文件时用 data/sample_cim.xml 验证。
stdlib xml.etree 实现，不引 rdflib 依赖（避免 requirements 增重）。

底层逻辑：CIM 用 ConnectivityNode + Terminal 建模电气连通——
Terminal 把一个 ConductingEquipment 连到一个 ConnectivityNode；
同一 ConnectivityNode 下的多个设备 = 电气相连（共节点）。
解析 → 设备节点 + 经 CN 派生的连通边 → 图。
"""
import xml.etree.ElementTree as ET
from collections import defaultdict

# CIM 61970 ConductingEquipment 子类（参与电气连通的设备；其余如 Substation/VoltageLevel 仅层级）
EQUIPMENT_TYPES = {
    "Breaker", "Disconnector", "LoadBreakSwitch", "Fuse",            # 开关
    "PowerTransformer", "ACLineSegment", "DCLineSegment",             # 变压/线路
    "BusbarSection",                                                  # 母线
    "EnergyConsumer", "SynchronousMachine", "AsynchronousMachine",   # 负荷/电源
    "ShuntCompensator", "StaticVarCompensator", "SeriesCompensator", # 补偿
    "CurrentTransformer", "PotentialTransformer",                     # 互感器
    "Ground", "SurgeArrester",                                        # 接地/避雷
}

_RDF_NS = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"


def _local(tag: str) -> str:
    """剥离命名空间：'{http://...}Breaker' → 'Breaker'。"""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _name(el) -> str:
    """取 IdentifiedObject.name 子元素文本（CIM 设备统一命名属性）。"""
    for child in el:
        if _local(child.tag) == "IdentifiedObject.name":
            return (child.text or "").strip()
    return ""


def _resource(el, prop_local: str) -> str:
    """取 rdf:resource 引用（如 Terminal.ConductingEquipment='#B1' → 'B1'）。"""
    for child in el:
        if _local(child.tag) == prop_local:
            ref = child.get(f"{_RDF_NS}resource", "")
            return ref.lstrip("#")
    return ""


def parse_cim_xml(xml_text: str) -> dict:
    """解析 CIMXML → {station, equipments, connectivityNodes, edges}。

    - equipments: [{mRID, name, type}]（EQUIPMENT_TYPES 命中的设备）
    - edges: [{a, b, via}]（a/b 为设备 mRID，via 为 ConnectivityNode mRID；同 CN 的设备两两相连）
    - 解析异常 → 抛 ValueError（调用方 degraded 兜底）
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"CIMXML 解析失败: {e}") from e

    equipments: dict[str, dict] = {}                  # mRID -> {mRID, name, type}
    cn_members: dict[str, list[str]] = defaultdict(list)  # CN mRID -> [设备 mRID]
    station = ""

    for el in root:
        tag = _local(el.tag)
        mrid = el.get("ID") or el.get(f"{_RDF_NS}ID") or ""
        if tag == "Substation":
            station = _name(el) or station
        elif tag in EQUIPMENT_TYPES and mrid:
            equipments[mrid] = {"mRID": mrid, "name": _name(el) or mrid, "type": tag}
        elif tag == "Terminal" and mrid:
            eq_ref = _resource(el, "Terminal.ConductingEquipment")
            cn_ref = _resource(el, "Terminal.ConnectivityNode")
            if eq_ref and cn_ref:
                cn_members[cn_ref].append(eq_ref)

    # 经 ConnectivityNode 派生连通边（同 CN 的设备两两相连；去重 + 去自环）
    edges = []
    seen: set[tuple[str, str]] = set()
    for cn, members in cn_members.items():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a == b:
                    continue
                key = tuple(sorted((a, b)))
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"a": a, "b": b, "via": cn})

    return {
        "station": station,
        "equipments": list(equipments.values()),
        "connectivityNodes": list(cn_members.keys()),
        "edges": edges,
    }


def graph_to_triples(graph: dict) -> list[tuple[str, str, str]]:
    """把连通图转成 (subject, "电气连接", object) 三元组列表（用设备名，供 KgTriple 存储 + GraphRAG 消费）。"""
    mrid_to_name = {e["mRID"]: e["name"] for e in graph["equipments"]}
    out = []
    for edge in graph["edges"]:
        a = mrid_to_name.get(edge["a"], edge["a"])
        b = mrid_to_name.get(edge["b"], edge["b"])
        out.append((a, "电气连接", b))
    return out
