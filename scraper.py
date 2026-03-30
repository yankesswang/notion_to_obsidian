import os
import re
import mimetypes
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

NOTION_API_KEY = "ntn_D5366895333b7pMYyRLqwVm3ZbsgeZIeyDMHrV1Lg4yfOR"
NOTION_VERSION = "2026-03-11"
PAGE_ID = "2dd13fe452e5806da040c424f49bf971"
OUTPUT_DIR = Path("output")
IMAGES_DIR = OUTPUT_DIR / "images"

BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# ── Notion API helpers ────────────────────────────────────────────────────────

def notion_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_all_children(block_id: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        try:
            data = notion_get(f"/blocks/{block_id}/children", params=params)
        except requests.exceptions.HTTPError as e:
            print(f"  [WARN] Could not fetch children of {block_id}: {e}")
            break
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def get_page_title(page_id: str) -> str:
    try:
        data = notion_get(f"/pages/{page_id}")
        props = data.get("properties", {})
        for prop in props.values():
            if prop.get("type") == "title":
                parts = prop.get("title", [])
                return "".join(p.get("plain_text", "") for p in parts)
    except Exception:
        pass
    return page_id


# ── Image download ───────────────────────────────────────────────────────────

def download_image(url: str, block_id: str) -> str:
    """Download image to IMAGES_DIR, return relative markdown path."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        # determine extension from Content-Type or URL path
        content_type = resp.headers.get("Content-Type", "")
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        if not ext or ext == ".jpe":
            # fall back to URL path extension
            path = urlparse(url).path
            ext = Path(path).suffix.split("?")[0] or ".png"

        filename = f"{block_id}{ext}"
        filepath = IMAGES_DIR / filename
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return f"images/{filename}"
    except Exception as e:
        print(f"  [WARN] Could not download image {block_id}: {e}")
        return url  # fall back to original URL


# ── Rich-text → plain text ────────────────────────────────────────────────────

def rich_text_to_md(rich_text: List[Dict[str, Any]]) -> str:
    parts = []
    for item in rich_text:
        text = item.get("plain_text", "")
        annotations = item.get("annotations", {})
        href = item.get("href")

        if annotations.get("code"):
            text = f"`{text}`"
        if annotations.get("bold"):
            text = f"**{text}**"
        if annotations.get("italic"):
            text = f"*{text}*"
        if annotations.get("strikethrough"):
            text = f"~~{text}~~"
        if href:
            text = f"[{text}]({href})"

        parts.append(text)
    return "".join(parts)


# ── Block → Markdown ──────────────────────────────────────────────────────────

def blocks_to_md(blocks: List[Dict[str, Any]], indent: int = 0) -> str:
    lines: List[str] = []
    prefix = "  " * indent
    numbered_counters: Dict[str, int] = {}  # track numbered list per nesting

    for block in blocks:
        btype = block.get("type", "")
        payload = block.get(btype, {})
        children = block.get("children", [])

        if btype == "paragraph":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}{text}" if text else "")

        elif btype == "heading_1":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{'#' * (1 + indent)} {text}")

        elif btype == "heading_2":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{'#' * (2 + indent)} {text}")

        elif btype == "heading_3":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{'#' * (3 + indent)} {text}")

        elif btype == "bulleted_list_item":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}- {text}")
            if children:
                lines.append(blocks_to_md(children, indent + 1))

        elif btype == "numbered_list_item":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}1. {text}")
            if children:
                lines.append(blocks_to_md(children, indent + 1))

        elif btype == "to_do":
            text = rich_text_to_md(payload.get("rich_text", []))
            checked = "x" if payload.get("checked") else " "
            lines.append(f"{prefix}- [{checked}] {text}")
            if children:
                lines.append(blocks_to_md(children, indent + 1))

        elif btype == "toggle":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}- {text}")
            if children:
                lines.append(blocks_to_md(children, indent + 1))

        elif btype == "callout":
            text = rich_text_to_md(payload.get("rich_text", []))
            icon = payload.get("icon", {})
            emoji = icon.get("emoji", "") if icon.get("type") == "emoji" else ""
            lines.append(f"{prefix}> {emoji} {text}".rstrip())
            if children:
                child_md = blocks_to_md(children, 0)
                for ln in child_md.splitlines():
                    lines.append(f"{prefix}> {ln}")

        elif btype == "quote":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}> {text}")
            if children:
                child_md = blocks_to_md(children, 0)
                for ln in child_md.splitlines():
                    lines.append(f"{prefix}> {ln}")

        elif btype == "code":
            text = rich_text_to_md(payload.get("rich_text", []))
            lang = payload.get("language", "")
            lines.append(f"```{lang}")
            lines.append(text)
            lines.append("```")

        elif btype == "divider":
            lines.append(f"{prefix}---")

        elif btype == "image":
            img = payload
            img_type = img.get("type", "")
            url = img.get(img_type, {}).get("url", "") if img_type else ""
            caption = rich_text_to_md(img.get("caption", []))
            alt = caption or "image"
            block_id = block.get("id", "img").replace("-", "")
            if url:
                local_path = download_image(url, block_id)
                lines.append(f"{prefix}![{alt}]({local_path})")
            else:
                lines.append(f"{prefix}![{alt}]")

        elif btype == "bookmark":
            url = payload.get("url", "")
            caption = rich_text_to_md(payload.get("caption", []))
            label = caption or url
            if url:
                lines.append(f"{prefix}[{label}]({url})")

        elif btype == "link_preview":
            url = payload.get("url", "")
            if url:
                lines.append(f"{prefix}[{url}]({url})")

        elif btype == "table":
            # children are table_rows; they were fetched recursively
            if children:
                for i, row_block in enumerate(children):
                    row_payload = row_block.get("table_row", {})
                    cells = row_payload.get("cells", [])
                    cell_texts = [rich_text_to_md(cell) for cell in cells]
                    lines.append(f"{prefix}| " + " | ".join(cell_texts) + " |")
                    if i == 0:
                        lines.append(f"{prefix}| " + " | ".join(["---"] * len(cells)) + " |")

        elif btype == "child_page":
            title = payload.get("title", "")
            child_id = block.get("id", "")
            safe = slugify(title) or child_id
            lines.append(f"{prefix}[[{safe}]]")

        elif btype == "child_database":
            title = payload.get("title", "")
            lines.append(f"{prefix}*[Database: {title}]*")

        elif btype == "embed":
            url = payload.get("url", "")
            lines.append(f"{prefix}[embed]({url})")

        elif btype == "video":
            vid = payload
            vid_type = vid.get("type", "")
            url = vid.get(vid_type, {}).get("url", "") if vid_type else ""
            lines.append(f"{prefix}[video]({url})")

        elif btype in ("unsupported",):
            lines.append(f"{prefix}<!-- unsupported block -->")

        else:
            # fallback: try to get rich_text if present
            rt = payload.get("rich_text", [])
            if rt:
                text = rich_text_to_md(rt)
                lines.append(f"{prefix}{text}")
            # skip blocks with no text (table_row handled inside table)

        if btype not in ("bulleted_list_item", "numbered_list_item", "to_do",
                          "toggle", "callout", "quote", "table") and children:
            lines.append(blocks_to_md(children, indent + 1))

    return "\n".join(lines)


# ── Filename helpers ──────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.strip()
    # replace characters that are problematic in filenames
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r'\s+', " ", text)
    return text[:100].strip()


# ── Main scraper ──────────────────────────────────────────────────────────────

def fetch_block_tree(block_id: str) -> List[Dict[str, Any]]:
    children = get_all_children(block_id)
    for block in children:
        if block.get("has_children") and block.get("type") != "child_page":
            block["children"] = fetch_block_tree(block["id"])
        else:
            block.setdefault("children", [])
    return children


def scrape_child_page(page_id: str, title: str, index: int, out_dir: Path) -> None:
    print(f"  [{index:02d}] Fetching: {title}")
    blocks = fetch_block_tree(page_id)
    md_body = blocks_to_md(blocks)
    md = f"# {title}\n\n{md_body}\n"

    filename = f"{index:02d}_{slugify(title)}.md"
    filepath = out_dir / filename
    filepath.write_text(md, encoding="utf-8")
    print(f"       Saved → {filepath}")


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)

    print(f"Fetching top-level blocks from page {PAGE_ID}...")
    top_blocks = get_all_children(PAGE_ID)

    # collect child_page blocks in order
    child_pages = [b for b in top_blocks if b.get("type") == "child_page"]
    print(f"Found {len(child_pages)} child pages.\n")

    for i, block in enumerate(child_pages, start=1):
        title = block.get("child_page", {}).get("title", "") or block["id"]
        scrape_child_page(block["id"], title, i, OUTPUT_DIR)

    print(f"\nDone. {len(child_pages)} files written to ./{OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
