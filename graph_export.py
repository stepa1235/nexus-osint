"""Export the project graph in interoperable formats."""
import json
from xml.etree.ElementTree import Element, SubElement, tostring


def serialize_graph(project, rows, graphml=False, entities=(), relationships=()):
    nodes = [{"id": "project", "label": project, "source": "", "kind": "project"}]
    edges = []
    for row in rows:
        node = f'evidence-{row["id"]}'
        nodes.append({"id": node, "label": row["title"], "source": row["source"], "kind": "evidence"})
        edges.append({"source": "project", "target": node, "relation": "contains"})
    for row in entities:
        nodes.append({"id": f'entity-{row["id"]}', "label": row["label"], "kind": row["kind"]})
    for row in relationships:
        edges.append({
            "source": f'entity-{row["origin"]}', "target": f'entity-{row["destination"]}',
            "relation": row["relation"], "confidence": row["confidence"],
            "evidence": f'evidence-{row["evidence"]}' if row["evidence"] is not None else "",
            "citation": row["source"] or "",
        })
    if not graphml:
        return json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, indent=2)

    attributes = {
        "node": ("label", "source", "kind"),
        "edge": ("relation", "confidence", "evidence", "citation"),
    }
    root = Element("graphml", xmlns="http://graphml.graphdrawing.org/xmlns")
    for scope, keys in attributes.items():
        for key in keys:
            SubElement(root, "key", {"id": key, "for": scope, "attr.name": key, "attr.type": "string"})
    graph = SubElement(root, "graph", edgedefault="directed")
    for node in nodes:
        element = SubElement(graph, "node", id=node["id"])
        for key in attributes["node"]:
            if key in node:
                SubElement(element, "data", key=key).text = node[key]
    for edge in edges:
        element = SubElement(graph, "edge", source=edge["source"], target=edge["target"])
        for key in attributes["edge"]:
            if key in edge:
                SubElement(element, "data", key=key).text = edge[key]
    return tostring(root, encoding="unicode")
