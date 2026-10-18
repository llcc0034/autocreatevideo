"""One bounded topology witness per declared destination, never an execution claim."""
from collections import deque


def _destinations(port, snapshot):
    if port["kind"] == "node_input":
        yield {"node_id": port["node_id"], "input_name": port["input_name"]}, False
        return
    if snapshot is None or port["kind"] != "semantic":
        return
    key = port["semantic_key"]
    for group in snapshot["input_contract"].get("asset_groups", []):
        if key not in group["semantic_keys"]:
            continue
        index = group["semantic_keys"].index(key)
        name = group["input_names"][index if group["wiring"] == "ordered_ports" else 0]
        for node in group["target_nodes"]:
            yield {"node_id": node, "input_name": name}, False
        return
    for binding in snapshot["semantic_bindings"]:
        if binding["semantic_key"] == key and binding["value_type"].startswith("asset_"):
            yield {"node_id": binding["node_id"], "input_name": binding["input_name"]}, True


def media_port_routes(ports, locations, edges, snapshot):
    """Edges are the verified original graph's shared list, not regenerated wiring.

    BFS returns one shortest witness, not every route or node execution order.
    Target input and output indices are retained; cycles cannot grow traversal.
    """
    adjacency = {}
    for index, edge in enumerate(edges):
        adjacency.setdefault(edge["from_node"], []).append((index, edge))
    result = []
    for declared in ports:
        targets = list(_destinations(declared["port"], snapshot))
        if not targets:
            result.append({"target_key": declared["target_key"], "destination": None,
                "state": "unavailable", "entry_node_id": None, "edge_indexes": [],
                "message": "精确语义端口合同不可核对；不按节点名称、素材名称或当前配置推断。"})
        for target, direct in targets:
            row = {"target_key": declared["target_key"], "destination": target, "state": "disconnected",
                "entry_node_id": None, "edge_indexes": [], "message": "原图未找到本次素材入口到此声明端口的连接；不是模型执行失败结论。"}
            if direct:
                found = next((p for p in locations if p["node_id"] == target["node_id"]
                    and p["input_path"] == [target["input_name"]]), None)
                if found:
                    row.update(state="connected", entry_node_id=found["node_id"],
                        message="本次素材直接写入冻结声明的加载端口，无中间图连接；不证明已上传或执行。")
            else:
                parents = {p["node_id"]: None for p in locations}
                queue = deque(parents)
                while queue:
                    node = queue.popleft()
                    if node == target["node_id"]:
                        continue  # A path through another target input is not evidence for this input.
                    for index, edge in adjacency.get(node, []):
                        if edge["to_node"] == target["node_id"] and edge["input_name"] == target["input_name"]:
                            path, start = [index], node
                            while parents[start] is not None:
                                parent, previous = parents[start]
                                path.append(previous)
                                start = parent
                            row.update(state="connected", entry_node_id=start, edge_indexes=list(reversed(path)),
                                message="原图存在一条最短连通路径；不是全部路径、处理算法、节点执行顺序或送达证明。")
                            break
                        if edge["to_node"] not in parents:
                            parents[edge["to_node"]] = (node, index)
                            queue.append(edge["to_node"])
                    if row["state"] == "connected":
                        break
            result.append(row)
    return result
