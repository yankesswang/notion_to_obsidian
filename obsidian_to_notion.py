"""
Obsidian / Markdown -> Notion importer

Config via .env (or environment variables):
    NOTION_API_KEY         Notion integration key (starts with ntn_ or secret_)
    NOTION_PARENT_PAGE_ID  Notion parent page ID where imported pages are created
    OBSIDIAN_VAULT_PATH    Absolute path to your Obsidian vault root
    OBSIDIAN_FOLDER_NAME   Folder name inside the vault to import from

CLI usage (overrides .env values):
    python importer.py [--key KEY] [--parent PAGE_ID]
                       [--vault /path/to/vault] [--folder "Folder Name"]
                       [--input ./local_folder]
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

NOTION_VERSION = "2026-03-11"
BASE_URL = "https://api.notion.com/v1"
MAX_RICH_TEXT = 2000


def notion_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(api_key: str, method: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.request(
        method,
        f"{BASE_URL}{path}",
        headers=notion_headers(api_key),
        json=payload,
        timeout=30,
    )
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        detail = ""
        try:
            data = resp.json()
            code = data.get("code")
            message = data.get("message")
            if code or message:
                detail = f" | Notion API: {code or 'unknown'} - {message or 'no message'}"
        except ValueError:
            if resp.text:
                detail = f" | Response: {resp.text[:500]}"
        raise requests.exceptions.HTTPError(f"{exc}{detail}", response=resp) from exc
    return resp.json()


def notion_post(api_key: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return notion_request(api_key, "POST", path, payload)


def notion_patch(api_key: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return notion_request(api_key, "PATCH", path, payload)


def slug_title_from_path(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"^\d+_", "", stem)
    stem = stem.replace("_", " ").strip()
    return stem or path.stem


def resolve_input_dir(args: argparse.Namespace) -> Path:
    if args.input:
        return Path(args.input)

    vault = args.vault or os.getenv("OBSIDIAN_VAULT_PATH", "")
    folder = args.folder or os.getenv("OBSIDIAN_FOLDER_NAME", "Notion Import")

    if not vault:
        print("Error: no input source.\n"
              "  Set OBSIDIAN_VAULT_PATH in .env, or pass --vault / --input.")
        sys.exit(1)

    input_dir = Path(vault) / folder
    if not input_dir.exists():
        print(f"Error: input path does not exist: {input_dir}")
        sys.exit(1)

    return input_dir


def md_files(input_dir: Path) -> List[Path]:
    return sorted(
        path for path in input_dir.rglob("*.md")
        if path.is_file()
    )


def split_segments(text: str, limit: int = MAX_RICH_TEXT) -> List[str]:
    if len(text) <= limit:
        return [text]

    parts: List[str] = []
    remaining = text
    while remaining:
        chunk = remaining[:limit]
        if len(remaining) > limit:
            split_at = max(chunk.rfind(" "), chunk.rfind("\n"))
            if split_at > 0:
                chunk = chunk[:split_at]
        parts.append(chunk)
        remaining = remaining[len(chunk):].lstrip()
    return parts


def text_annotations(**kwargs: Any) -> Dict[str, Any]:
    return {
        "bold": kwargs.get("bold", False),
        "italic": kwargs.get("italic", False),
        "strikethrough": kwargs.get("strikethrough", False),
        "underline": False,
        "code": kwargs.get("code", False),
        "color": "default",
    }


def rich_text_item(text: str, href: Optional[str] = None, **annotations: Any) -> Dict[str, Any]:
    text_obj: Dict[str, Any] = {"content": text}
    if href:
        text_obj["link"] = {"url": href}
    return {
        "type": "text",
        "text": text_obj,
        "annotations": text_annotations(**annotations),
    }


def parse_inline(text: str, page_links: Dict[str, str]) -> List[Dict[str, Any]]:
    tokens: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"(\[\[[^\]]+\]\]|\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|~~([^~]+)~~|\*([^*]+)\*)"
    )
    pos = 0

    for match in pattern.finditer(text):
        if match.start() > pos:
            tokens.extend(rich_text_item(part) for part in split_segments(text[pos:match.start()]))

        raw = match.group(0)
        wiki = match.group(1) if raw.startswith("[[") else None
        link_label = match.group(2)
        link_url = match.group(3)
        code_text = match.group(4)
        bold_text = match.group(5)
        strike_text = match.group(6)
        italic_text = match.group(7)

        if wiki:
            title = wiki[2:-2].strip()
            href = page_links.get(title)
            tokens.extend(rich_text_item(part, href=href) for part in split_segments(title))
        elif link_label is not None and link_url is not None:
            tokens.extend(rich_text_item(part, href=link_url) for part in split_segments(link_label))
        elif code_text is not None:
            tokens.extend(rich_text_item(part, code=True) for part in split_segments(code_text))
        elif bold_text is not None:
            tokens.extend(rich_text_item(part, bold=True) for part in split_segments(bold_text))
        elif strike_text is not None:
            tokens.extend(rich_text_item(part, strikethrough=True) for part in split_segments(strike_text))
        elif italic_text is not None:
            tokens.extend(rich_text_item(part, italic=True) for part in split_segments(italic_text))

        pos = match.end()

    if pos < len(text):
        tokens.extend(rich_text_item(part) for part in split_segments(text[pos:]))

    return tokens


def paragraph_block(text: str, page_links: Dict[str, str]) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": parse_inline(text, page_links),
        },
    }


def heading_block(level: int, text: str, page_links: Dict[str, str]) -> Dict[str, Any]:
    block_type = f"heading_{min(level, 3)}"
    return {
        "object": "block",
        "type": block_type,
        block_type: {
            "rich_text": parse_inline(text, page_links),
        },
    }


def list_item_block(block_type: str, text: str, page_links: Dict[str, str], checked: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "rich_text": parse_inline(text, page_links),
    }
    if block_type == "to_do":
        payload["checked"] = checked
    return {
        "object": "block",
        "type": block_type,
        block_type: payload,
    }


NOTION_LANGUAGES = {
    "abap", "abc", "agda", "arduino", "ascii art", "assembly", "bash", "basic", "bnf",
    "c", "c#", "c++", "clojure", "coffeescript", "coq", "css", "dart", "dhall", "diff",
    "docker", "ebnf", "elixir", "elm", "erlang", "f#", "flow", "fortran", "gherkin",
    "glsl", "go", "graphql", "groovy", "haskell", "hcl", "html", "idris", "java",
    "javascript", "json", "julia", "kotlin", "latex", "less", "lisp", "livescript",
    "llvm ir", "lua", "makefile", "markdown", "markup", "matlab", "mathematica",
    "mermaid", "nix", "notion formula", "objective-c", "ocaml", "pascal", "perl",
    "php", "plain text", "powershell", "prolog", "protobuf", "purescript", "python",
    "r", "racket", "reason", "ruby", "rust", "sass", "scala", "scheme", "scss",
    "shell", "smalltalk", "solidity", "sql", "swift", "toml", "typescript", "vb.net",
    "verilog", "vhdl", "visual basic", "webassembly", "xml", "yaml", "java/c/c++/c#",
}


def normalize_language(language: str) -> str:
    lang = language.lower().strip()
    if lang in NOTION_LANGUAGES:
        return lang
    # common aliases
    aliases = {"sh": "bash", "js": "javascript", "ts": "typescript", "py": "python",
               "rb": "ruby", "text": "plain text", "txt": "plain text", "": "plain text"}
    return aliases.get(lang, "plain text")


def code_block(language: str, code: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "code",
        "code": {
            "language": normalize_language(language),
            "rich_text": [rich_text_item(part) for part in split_segments(code)],
        },
    }


def quote_block(text: str, page_links: Dict[str, str]) -> Dict[str, Any]:
    # Strip Obsidian callout tag [!type] and the single space/newline after it.
    # \s? (not \s*) so we don't swallow the content on the same line.
    text = re.sub(r"^\[![^\]]+\] ?", "", text)
    return {
        "object": "block",
        "type": "quote",
        "quote": {
            "rich_text": parse_inline(text, page_links),
        },
    }


def divider_block() -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "divider",
        "divider": {},
    }


def bookmark_block(url: str, label: str, page_links: Dict[str, str]) -> Dict[str, Any]:
    caption = parse_inline(label, page_links) if label and label != url else []
    return {
        "object": "block",
        "type": "bookmark",
        "bookmark": {
            "url": url,
            "caption": caption,
        },
    }


def table_block(rows: List[List[str]], page_links: Dict[str, str]) -> Dict[str, Any]:
    width = max(len(r) for r in rows) if rows else 1
    table_rows = []
    for row in rows:
        # Pad short rows; trim long rows
        padded = (row + [""] * width)[:width]
        cells = [parse_inline(cell.strip(), page_links) for cell in padded]
        table_rows.append({
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": cells},
        })
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": table_rows,
        },
    }


def parse_table_row(line: str) -> List[str]:
    """Split a markdown table row into cells, stripping leading/trailing pipes."""
    line = line.strip().strip("|")
    return [cell for cell in line.split("|")]


def image_block(src: str, alt: str, page_links: Dict[str, str]) -> Dict[str, Any]:
    if src.startswith("http://") or src.startswith("https://"):
        return {
            "object": "block",
            "type": "image",
            "image": {
                "type": "external",
                "external": {"url": src},
                "caption": parse_inline(alt, page_links) if alt else [],
            },
        }
    note = f"[local image skipped] {alt or src} -> {src}"
    return paragraph_block(note, page_links)


def append_children(api_key: str, block_id: str, children: List[Dict[str, Any]]) -> None:
    for i in range(0, len(children), 100):
        notion_patch(api_key, f"/blocks/{block_id}/children", {"children": children[i:i + 100]})


def create_child_page(api_key: str, parent_page_id: str, title: str) -> Dict[str, Any]:
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "properties": {
            "title": {
                "title": [rich_text_item(title)],
            }
        },
    }
    return notion_post(api_key, "/pages", payload)


def parse_list(lines: List[str], start: int, page_links: Dict[str, str], base_indent: int) -> Tuple[List[Dict[str, Any]], int]:
    items: List[Dict[str, Any]] = []
    index = start

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            break

        indent = len(line) - len(line.lstrip(" "))
        if indent < base_indent:
            break

        stripped = line.strip()
        todo_match = re.match(r"[-*] \[( |x|X)\] (.+)", stripped)
        bullet_match = re.match(r"[-*] (.+)", stripped)
        number_match = re.match(r"\d+\. (.+)", stripped)

        if indent != base_indent or not (todo_match or bullet_match or number_match):
            break

        if todo_match:
            block = list_item_block("to_do", todo_match.group(2), page_links, checked=todo_match.group(1).lower() == "x")
        elif number_match:
            block = list_item_block("numbered_list_item", number_match.group(1), page_links)
        else:
            block = list_item_block("bulleted_list_item", bullet_match.group(1), page_links)

        index += 1
        if index < len(lines):
            next_line = lines[index]
            next_indent = len(next_line) - len(next_line.lstrip(" "))
            if next_line.strip() and next_indent > base_indent:
                children, index = parse_list(lines, index, page_links, next_indent)
                if children:
                    block[block["type"]]["children"] = children

        items.append(block)

    return items, index


def parse_markdown_blocks(content: str, page_links: Dict[str, str]) -> List[Dict[str, Any]]:
    lines = content.splitlines()
    blocks: List[Dict[str, Any]] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            language = stripped[3:].strip()
            index += 1
            code_lines: List[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(code_block(language, "\n".join(code_lines)))
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            blocks.append(heading_block(len(heading.group(1)), heading.group(2), page_links))
            index += 1
            continue

        if stripped == "---":
            blocks.append(divider_block())
            index += 1
            continue

        if stripped.startswith(">"):
            quote_lines: List[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(lines[index].strip()[1:].lstrip())
                index += 1
            blocks.append(quote_block("\n".join(quote_lines), page_links))
            continue

        image_match = re.match(r"!\[(.*?)\]\((.+?)\)$", stripped)
        if image_match:
            blocks.append(image_block(image_match.group(2), image_match.group(1), page_links))
            index += 1
            continue

        standalone_link = re.match(r"^\[(.+?)\]\((https?://.+)\)$", stripped)
        if standalone_link:
            blocks.append(bookmark_block(standalone_link.group(2), standalone_link.group(1), page_links))
            index += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            table_rows: List[List[str]] = []
            while index < len(lines):
                row_line = lines[index].strip()
                if not (row_line.startswith("|") and row_line.endswith("|")):
                    break
                # skip separator rows like |---|---|
                if re.match(r"^\|[-| :]+\|$", row_line):
                    index += 1
                    continue
                table_rows.append(parse_table_row(row_line))
                index += 1
            if table_rows:
                blocks.append(table_block(table_rows, page_links))
            continue

        list_match = re.match(r"^(\s*)([-*] \[(?: |x|X)\] .+|[-*] .+|\d+\. .+)$", line)
        if list_match:
            base_indent = len(list_match.group(1))
            items, index = parse_list(lines, index, page_links, base_indent)
            blocks.extend(items)
            continue

        para_lines = [stripped]
        index += 1
        while index < len(lines):
            next_line = lines[index]
            next_stripped = next_line.strip()
            if not next_stripped:
                break
            if re.match(r"^(#{1,3})\s+.+$", next_stripped):
                break
            if next_stripped.startswith(("```", ">", "![", "---")):
                break
            if re.match(r"^(\s*)([-*] \[(?: |x|X)\] .+|[-*] .+|\d+\. .+)$", next_line):
                break
            if re.match(r"^\[(.+?)\]\((https?://.+)\)$", next_stripped):
                break
            para_lines.append(next_stripped)
            index += 1
        blocks.append(paragraph_block(" ".join(para_lines), page_links))

    return blocks


def extract_title_and_body(path: Path) -> Tuple[str, str]:
    content = path.read_text(encoding="utf-8")
    # Strip YAML frontmatter
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            content = content[end + 4:].lstrip("\n")
    lines = content.splitlines()
    if lines and re.match(r"^#\s+.+$", lines[0].strip()):
        title = lines[0].strip()[2:].strip()
        body = "\n".join(lines[1:]).lstrip("\n")
        return title, body
    return slug_title_from_path(path), content


def notion_page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Markdown files from Obsidian or a local folder into Notion child pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--key", help="Notion API key (overrides .env NOTION_API_KEY)")
    parser.add_argument("--parent", help="Target parent page ID (overrides .env NOTION_PARENT_PAGE_ID)")
    parser.add_argument("--vault", help="Obsidian vault path (overrides .env OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--folder", help="Folder name inside vault (overrides .env OBSIDIAN_FOLDER_NAME)")
    parser.add_argument("--input", help="Import from this local directory instead of an Obsidian vault")
    args = parser.parse_args()

    api_key = args.key or os.getenv("NOTION_API_KEY", "")
    parent_page_id = args.parent or os.getenv("NOTION_PARENT_PAGE_ID", "")

    if not api_key:
        print("Error: NOTION_API_KEY not set. Add it to .env or pass --key.")
        sys.exit(1)
    if not parent_page_id:
        print("Error: NOTION_PARENT_PAGE_ID not set. Add it to .env or pass --parent.")
        sys.exit(1)

    input_dir = resolve_input_dir(args)
    files = md_files(input_dir)

    if not files:
        print(f"Error: no markdown files found in {input_dir}")
        sys.exit(1)

    print(f"Input       : {input_dir}")
    print(f"Parent page : {parent_page_id}")
    print(f"Files       : {len(files)}")
    print()

    # Build folder pages for every subdirectory, shallowest first.
    # folder_pages maps relative dir Path -> Notion page ID
    folder_pages: Dict[Path, str] = {Path("."): parent_page_id.strip()}

    subdirs: List[Path] = sorted({
        f.relative_to(input_dir).parent
        for f in files
        if f.relative_to(input_dir).parent != Path(".")
    })
    for rel_dir in subdirs:
        # Ensure all ancestor folders are already created
        for depth in range(1, len(rel_dir.parts) + 1):
            ancestor = Path(*rel_dir.parts[:depth])
            if ancestor not in folder_pages:
                parent_of_ancestor = Path(*rel_dir.parts[:depth - 1]) if depth > 1 else Path(".")
                folder_name = rel_dir.parts[depth - 1]
                print(f"[folder] Creating: {ancestor}")
                try:
                    created = create_child_page(api_key, folder_pages[parent_of_ancestor], folder_name)
                except requests.exceptions.HTTPError as exc:
                    print(f"Error creating folder page '{folder_name}': {exc}")
                    sys.exit(1)
                folder_pages[ancestor] = created["id"]

    print()

    # Collect docs with their target parent page ID
    docs: List[Dict[str, Any]] = []
    for path in files:
        title, body = extract_title_and_body(path)
        rel_parent = path.relative_to(input_dir).parent
        docs.append({
            "path": path,
            "title": title,
            "body": body,
            "parent_id": folder_pages[rel_parent],
        })

    page_links: Dict[str, str] = {}
    for index, doc in enumerate(docs, start=1):
        print(f"[{index:02d}] Creating page: {doc['title']}")
        try:
            created = create_child_page(api_key, doc["parent_id"], doc["title"])
        except requests.exceptions.HTTPError as exc:
            print(f"Error creating page under parent {doc['parent_id']}: {exc}")
            print("Check that the integration has access to the parent page and can insert content.")
            sys.exit(1)
        doc["page_id"] = created["id"]
        page_links[doc["title"]] = notion_page_url(created["id"])

    print()
    for index, doc in enumerate(docs, start=1):
        print(f"[{index:02d}] Writing blocks: {doc['title']}")
        blocks = parse_markdown_blocks(doc["body"], page_links)
        if not blocks:
            print("     -> skipped (empty body)")
            continue
        try:
            append_children(api_key, doc["page_id"], blocks)
        except requests.exceptions.HTTPError as exc:
            print(f"Error writing blocks to page {doc['title']}: {exc}")
            sys.exit(1)
        print(f"     -> {len(blocks)} top-level blocks")

    print(f"\nDone. Imported {len(docs)} pages into Notion.")


if __name__ == "__main__":
    main()
