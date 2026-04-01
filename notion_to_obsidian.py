"""
Notion → Obsidian / Markdown scraper

Config via .env (or environment variables):
    NOTION_API_KEY        Notion integration key (starts with ntn_ or secret_)
    NOTION_PAGE_ID        Notion page ID to export
    OBSIDIAN_VAULT_PATH   Absolute path to your Obsidian vault root
    OBSIDIAN_FOLDER_NAME  Folder name inside the vault (created if absent)

CLI usage (overrides .env values):
    python main.py [--key KEY] [--page PAGE_ID]
                   [--vault /path/to/vault] [--folder "Folder Name"]
                   [--output ./local_folder]   # saves locally instead of vault

Examples:
    # use .env, save to Obsidian
    python main.py

    # override page, save to Obsidian from .env
    python main.py --page abc123

    # save locally (no Obsidian)
    python main.py --output ./output
"""

import argparse
import mimetypes
import os
import re
import sys
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

NOTION_VERSION = "2026-03-11"
BASE_URL = "https://api.notion.com/v1"


# ── Notion API ────────────────────────────────────────────────────────────────

def notion_get(api_key: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_all_children(api_key: str, block_id: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        try:
            data = notion_get(api_key, f"/blocks/{block_id}/children", params=params)
        except requests.exceptions.HTTPError as e:
            print(f"  [WARN] Could not fetch children of {block_id}: {e}")
            break
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def fetch_block_tree(api_key: str, block_id: str) -> List[Dict[str, Any]]:
    children = get_all_children(api_key, block_id)
    for block in children:
        if block.get("has_children") and block.get("type") != "child_page":
            block["children"] = fetch_block_tree(api_key, block["id"])
        else:
            block.setdefault("children", [])
    return children


# ── Image download ────────────────────────────────────────────────────────────

def download_image(url: str, block_id: str, images_dir: Path) -> str:
    images_dir.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        if not ext or ext == ".jpe":
            path = urlparse(url).path
            ext = Path(path).suffix.split("?")[0] or ".png"

        filename = f"{block_id}{ext}"
        with open(images_dir / filename, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return f"images/{filename}"
    except Exception as e:
        print(f"  [WARN] Could not download image {block_id}: {e}")
        return url


# ── Rich text → Markdown ──────────────────────────────────────────────────────

def rich_text_to_md(rich_text: List[Dict[str, Any]]) -> str:
    parts = []
    for item in rich_text:
        text = item.get("plain_text", "")
        ann = item.get("annotations", {})
        href = item.get("href")
        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("bold"):
            text = f"**{text}**"
        if ann.get("italic"):
            text = f"*{text}*"
        if ann.get("strikethrough"):
            text = f"~~{text}~~"
        if href:
            text = f"[{text}]({href})"
        parts.append(text)
    return "".join(parts)


# ── Blocks → Markdown ─────────────────────────────────────────────────────────

def blocks_to_md(blocks: List[Dict[str, Any]], images_dir: Path, indent: int = 0) -> str:
    lines: List[str] = []
    prefix = "  " * indent

    for block in blocks:
        btype = block.get("type", "")
        payload = block.get(btype, {})
        children = block.get("children", [])

        if btype == "paragraph":
            text = rich_text_to_md(payload.get("rich_text", []))
            lines.append(f"{prefix}{text}" if text else "")

        elif btype == "heading_1":
            lines.append(f"{'#' * (1 + indent)} {rich_text_to_md(payload.get('rich_text', []))}")

        elif btype == "heading_2":
            lines.append(f"{'#' * (2 + indent)} {rich_text_to_md(payload.get('rich_text', []))}")

        elif btype == "heading_3":
            lines.append(f"{'#' * (3 + indent)} {rich_text_to_md(payload.get('rich_text', []))}")

        elif btype == "bulleted_list_item":
            lines.append(f"{prefix}- {rich_text_to_md(payload.get('rich_text', []))}")
            if children:
                lines.append(blocks_to_md(children, images_dir, indent + 1))

        elif btype == "numbered_list_item":
            lines.append(f"{prefix}1. {rich_text_to_md(payload.get('rich_text', []))}")
            if children:
                lines.append(blocks_to_md(children, images_dir, indent + 1))

        elif btype == "to_do":
            checked = "x" if payload.get("checked") else " "
            lines.append(f"{prefix}- [{checked}] {rich_text_to_md(payload.get('rich_text', []))}")
            if children:
                lines.append(blocks_to_md(children, images_dir, indent + 1))

        elif btype == "toggle":
            lines.append(f"{prefix}- {rich_text_to_md(payload.get('rich_text', []))}")
            if children:
                lines.append(blocks_to_md(children, images_dir, indent + 1))

        elif btype == "callout":
            icon = payload.get("icon", {})
            emoji = icon.get("emoji", "") if icon.get("type") == "emoji" else ""
            lines.append(f"{prefix}> {emoji} {rich_text_to_md(payload.get('rich_text', []))}".rstrip())
            if children:
                for ln in blocks_to_md(children, images_dir, 0).splitlines():
                    lines.append(f"{prefix}> {ln}")

        elif btype == "quote":
            lines.append(f"{prefix}> {rich_text_to_md(payload.get('rich_text', []))}")
            if children:
                for ln in blocks_to_md(children, images_dir, 0).splitlines():
                    lines.append(f"{prefix}> {ln}")

        elif btype == "code":
            lang = payload.get("language", "")
            lines.append(f"```{lang}")
            lines.append(rich_text_to_md(payload.get("rich_text", [])))
            lines.append("```")

        elif btype == "divider":
            lines.append(f"{prefix}---")

        elif btype == "image":
            img_type = payload.get("type", "")
            url = payload.get(img_type, {}).get("url", "") if img_type else ""
            caption = rich_text_to_md(payload.get("caption", []))
            alt = caption or "image"
            block_id = block.get("id", "img").replace("-", "")
            if url:
                local_path = download_image(url, block_id, images_dir)
                lines.append(f"{prefix}![{alt}]({local_path})")
            else:
                lines.append(f"{prefix}![{alt}]")

        elif btype == "bookmark":
            url = payload.get("url", "")
            label = rich_text_to_md(payload.get("caption", [])) or url
            if url:
                lines.append(f"{prefix}[{label}]({url})")

        elif btype == "link_preview":
            url = payload.get("url", "")
            if url:
                lines.append(f"{prefix}[{url}]({url})")

        elif btype == "table":
            if children:
                for i, row_block in enumerate(children):
                    cells = row_block.get("table_row", {}).get("cells", [])
                    cell_texts = [rich_text_to_md(cell) for cell in cells]
                    lines.append(f"{prefix}| " + " | ".join(cell_texts) + " |")
                    if i == 0:
                        lines.append(f"{prefix}| " + " | ".join(["---"] * len(cells)) + " |")

        elif btype == "child_page":
            title = payload.get("title", "")
            safe = slugify(title) or block.get("id", "")
            lines.append(f"{prefix}[[{safe}]]")

        elif btype == "child_database":
            lines.append(f"{prefix}*[Database: {payload.get('title', '')}]*")

        elif btype == "embed":
            lines.append(f"{prefix}[embed]({payload.get('url', '')})")

        elif btype == "video":
            vid_type = payload.get("type", "")
            url = payload.get(vid_type, {}).get("url", "") if vid_type else ""
            lines.append(f"{prefix}[video]({url})")

        elif btype == "unsupported":
            lines.append(f"{prefix}<!-- unsupported block -->")

        else:
            rt = payload.get("rich_text", [])
            if rt:
                lines.append(f"{prefix}{rich_text_to_md(rt)}")

        if btype not in ("bulleted_list_item", "numbered_list_item", "to_do",
                         "toggle", "callout", "quote", "table") and children:
            lines.append(blocks_to_md(children, images_dir, indent + 1))

    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.strip()
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r'\s+', " ", text)
    return text[:100].strip()


def resolve_output_dir(args: argparse.Namespace) -> Path:
    """Return the directory where .md files will be written."""
    if args.output:
        return Path(args.output)

    vault = args.vault or os.getenv("OBSIDIAN_VAULT_PATH", "")
    folder = args.folder or os.getenv("OBSIDIAN_FOLDER_NAME", "Notion Import")

    if not vault:
        print("Error: no output destination.\n"
              "  Set OBSIDIAN_VAULT_PATH in .env, or pass --vault / --output.")
        sys.exit(1)

    vault_path = Path(vault)
    if not vault_path.exists():
        print(f"Error: Obsidian vault path does not exist: {vault_path}")
        sys.exit(1)

    return vault_path / folder


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a Notion page to Markdown and save to Obsidian or a local folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--key",    help="Notion API key (overrides .env NOTION_API_KEY)")
    parser.add_argument("--page",   help="Notion page ID (overrides .env NOTION_PAGE_ID)")
    parser.add_argument("--vault",  help="Obsidian vault path (overrides .env OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--folder", help="Folder name inside vault (overrides .env OBSIDIAN_FOLDER_NAME)")
    parser.add_argument("--output", help="Save locally to this path instead of Obsidian vault")
    args = parser.parse_args()

    api_key = args.key or os.getenv("NOTION_API_KEY", "")
    page_id = args.page or os.getenv("NOTION_PAGE_ID", "")

    if not api_key:
        print("Error: NOTION_API_KEY not set. Add it to .env or pass --key.")
        sys.exit(1)
    if not page_id:
        print("Error: NOTION_PAGE_ID not set. Add it to .env or pass --page.")
        sys.exit(1)

    out_dir = resolve_output_dir(args)
    images_dir = out_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    dest_label = str(out_dir)
    print(f"Notion page : {page_id}")
    print(f"Destination : {dest_label}")
    print()

    try:
        top_blocks = get_all_children(api_key, page_id)
    except requests.exceptions.HTTPError as e:
        print(f"Error fetching page: {e}")
        sys.exit(1)

    child_pages = [b for b in top_blocks if b.get("type") == "child_page"]

    if not child_pages:
        print("No child pages found — exporting the page itself.")
        blocks = fetch_block_tree(api_key, page_id)
        md_body = blocks_to_md(blocks, images_dir)
        (out_dir / "page.md").write_text(f"{md_body}\n", encoding="utf-8")
        print(f"Saved → {out_dir / 'page.md'}")
        return

    print(f"Found {len(child_pages)} child pages.\n")
    for i, block in enumerate(child_pages, start=1):
        title = block.get("child_page", {}).get("title", "") or block["id"]
        print(f"  [{i:02d}] {title}")
        blocks = fetch_block_tree(api_key, block["id"])
        md_body = blocks_to_md(blocks, images_dir)
        md = f"# {title}\n\n{md_body}\n"
        filename = f"{i:02d}_{slugify(title)}.md"
        filepath = out_dir / filename
        filepath.write_text(md, encoding="utf-8")
        print(f"       → {filepath}")

    print(f"\nDone. {len(child_pages)} files written to {out_dir}/")


if __name__ == "__main__":
    main()
