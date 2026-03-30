import os
import requests
from typing import Any, Dict, List, Optional

# NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_API_KEY  = "ntn_D5366895333b7pMYyRLqwVm3ZbsgeZIeyDMHrV1Lg4yfOR"
NOTION_VERSION = "2026-03-11"
PAGE_ID = "2dd13fe452e5806da040c424f49bf971"
# os.environ["NOTION_PAGE_ID"]

BASE_URL = "https://api.notion.com/v1"

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def notion_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_all_block_children(block_id: str) -> List[Dict[str, Any]]:
    """
    取得某個 block 的所有 children，會自動處理分頁。
    page_id 也可以直接當作 block_id 傳進來。
    """
    results: List[Dict[str, Any]] = []
    start_cursor: Optional[str] = None

    while True:
        params: Dict[str, Any] = {"page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor

        try:
            data = notion_get(f"/blocks/{block_id}/children", params=params)
        except requests.exceptions.HTTPError as e:
            print(f"  [WARN] Failed to fetch children for {block_id}: {e}")
            break

        results.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return results


def build_block_tree(block_id: str) -> List[Dict[str, Any]]:
    """
    遞迴建立 block tree。
    對 has_children=True 的 block 會再往下抓。
    """
    children = list_all_block_children(block_id)
    tree: List[Dict[str, Any]] = []

    for block in children:
        node = dict(block)  # shallow copy

        if block.get("has_children"):
            node["children"] = build_block_tree(block["id"])
        else:
            node["children"] = []

        tree.append(node)

    return tree


def get_page_content(page_id: str) -> List[Dict[str, Any]]:
    """
    取得頁面正文內容。
    根據官方文件，page_id 可直接當作 block_id 使用。
    """
    return build_block_tree(page_id)


def find_child_pages(blocks: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    從 block tree 中找出所有 child_page。
    """
    found: List[Dict[str, str]] = []

    def walk(nodes: List[Dict[str, Any]]) -> None:
        for node in nodes:
            if node.get("type") == "child_page":
                found.append({
                    "id": node["id"],
                    "title": node.get("child_page", {}).get("title", "")
                })

            if node.get("children"):
                walk(node["children"])

    walk(blocks)
    return found


def extract_plain_text_from_rich_text(rich_text: List[Dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", "") for item in rich_text)


def print_blocks(blocks: List[Dict[str, Any]], indent: int = 0) -> None:
    """
    簡單把常見 block 印成可讀文字。
    """
    prefix = "  " * indent

    for block in blocks:
        block_type = block.get("type", "unknown")

        if block_type in ("paragraph", "heading_1", "heading_2", "heading_3",
                          "bulleted_list_item", "numbered_list_item", "quote",
                          "to_do", "toggle", "callout"):
            payload = block.get(block_type, {})
            text = extract_plain_text_from_rich_text(payload.get("rich_text", []))
            print(f"{prefix}- [{block_type}] {text}")

        elif block_type == "child_page":
            title = block.get("child_page", {}).get("title", "")
            print(f"{prefix}- [child_page] {title}")

        else:
            print(f"{prefix}- [{block_type}]")

        if block.get("children"):
            print_blocks(block["children"], indent + 1)


if __name__ == "__main__":
    # 1. 抓頁面內容
    content = get_page_content(PAGE_ID)

    # 2. 印出簡單內容
    print("=== PAGE CONTENT ===")
    print_blocks(content)

    # 3. 找出所有 sub pages
    sub_pages = find_child_pages(content)
    print("\n=== CHILD PAGES ===")
    for sp in sub_pages:
        print(f'- {sp["title"]} ({sp["id"]})')
