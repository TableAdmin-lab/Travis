#!/usr/bin/env python3
"""
All-in-one local WBR automation script.

This is a single flat script. It does not embed or nest other scripts.

It will:
1. Find the WBR template item inside a Notion database.
2. Create a new WBR database item for the reporting week.
3. Copy the template structure, including tables.
4. Skip old images/graphs.
5. Read four CSVs from the reporting week folder:
   - first-line-inbound-weekly
   - first-line-inbound-8-week
   - first-line-outbound-8-week
   - first-line-chats
6. Generate four chart images:
   - 1 Week View
   - Inbound
   - Outbound
   - Chats
7. Upload the chart images to Notion.
8. Insert each chart into the copied WBR item under:
   17.7 First Line Support, after the matching section/anchor text.

Required env:
  NOTION_TOKEN

Recommended env:
  NOTION_DATABASE_ID="379ba43eb1358028b426ebe91903abaa"
  TEMPLATE_ITEM_TITLE="TESTING Weekly Business Review"
  REPORTING_WEEK="2026-06-15"
  NEW_WBR_TITLE="WBR - 2026-06-15 Local Test"

Optional CSV env:
  CSV_INBOUND_WEEKLY_PATH
  CSV_INBOUND_8_WEEK_PATH
  CSV_OUTBOUND_8_WEEK_PATH
  CSV_CHATS_PATH
  CSV_DIR

Modes:
  WBR_AUTOMATION_MODE=full        -> copy WBR item and insert charts
  WBR_AUTOMATION_MODE=copy_only   -> only copy WBR item
  WBR_AUTOMATION_MODE=charts_only -> only insert charts into NOTION_PAGE_ID / NEW_NOTION_PAGE_ID
"""

import copy
import csv
import math
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFont


# =============================================================================
# CONFIG
# =============================================================================

BASE_URL = "https://api.notion.com/v1"
BUILD_WBR_VERSION = "2026-06-09-two-chat-graphs-v9"

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")

# Can be either the database ID or a page containing an inline database.
NOTION_DATABASE_ID = os.getenv(
    "NOTION_DATABASE_ID",
    "379ba43eb1358028b426ebe91903abaa",
)
NOTION_CHILD_DATABASE_ID = os.getenv("NOTION_CHILD_DATABASE_ID")

TEMPLATE_ITEM_TITLE = os.getenv(
    "TEMPLATE_ITEM_TITLE",
    "WBR Template",
)
TEMPLATE_ITEM_PAGE_ID = os.getenv("TEMPLATE_ITEM_PAGE_ID")

REPORTING_WEEK = os.getenv("REPORTING_WEEK")
NEW_WBR_TITLE = os.getenv("NEW_WBR_TITLE")

COPY_BLOCKS = os.getenv("COPY_BLOCKS", "true").lower() == "true"
SKIP_IMAGES = os.getenv("SKIP_IMAGES", "true").lower() == "true"
UPDATE_DATE_PROPERTIES = os.getenv("UPDATE_DATE_PROPERTIES", "true").lower() == "true"
MAX_COPY_DEPTH = int(os.getenv("MAX_COPY_DEPTH", "8"))

WBR_AUTOMATION_MODE = os.getenv("WBR_AUTOMATION_MODE", "full").lower().strip()

# Chart / CSV config
CSV_DIR = Path(os.getenv("CSV_DIR", "."))
CSV_INBOUND_WEEKLY_PATH = os.getenv("CSV_INBOUND_WEEKLY_PATH")
CSV_INBOUND_8_WEEK_PATH = os.getenv("CSV_INBOUND_8_WEEK_PATH")
CSV_OUTBOUND_8_WEEK_PATH = os.getenv("CSV_OUTBOUND_8_WEEK_PATH")
CSV_CHATS_PATH = os.getenv("CSV_CHATS_PATH")

TARGET_HEADING = os.getenv("TARGET_HEADING", "17.7 First Line Support")
TARGET_CHATS_HEADING = os.getenv("TARGET_CHATS_HEADING", "17.8")
# Default anchors expected inside the First Line Support section. Override these
# in GitHub Actions if the exact Notion template text is different.
TARGET_INBOUND_WEEKLY_ANCHOR_TEXT = os.getenv("TARGET_INBOUND_WEEKLY_ANCHOR_TEXT", "New Daily 1 Week View")
TARGET_INBOUND_8_WEEK_ANCHOR_TEXT = os.getenv("TARGET_INBOUND_8_WEEK_ANCHOR_TEXT", "Inbound")
TARGET_OUTBOUND_8_WEEK_ANCHOR_TEXT = os.getenv("TARGET_OUTBOUND_8_WEEK_ANCHOR_TEXT", "Outbound")
TARGET_CHATS_ANCHOR_TEXT = os.getenv("TARGET_CHATS_ANCHOR_TEXT", "Chats")
# Backward-compatible alias used by older local runs.
TARGET_ANCHOR_TEXT = os.getenv("TARGET_ANCHOR_TEXT", TARGET_INBOUND_WEEKLY_ANCHOR_TEXT)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "wbr_outputs/first_line_charts"))

DATE_FIELD = "Interaction Date Dynamic"
ELIGIBLE_CALLS_FIELD = "Eligible Calls [#]"
AI_CSAT_FIELD = "AI CSAT Call [%]"
AI_CSAT_PARTICIPATION_FIELD = "AI CSAT Call Participation Rate [%]"
ABANDONED_FIELD = "Abandoned Call Rate [%]"

AUTO_CHARTS_START = "AUTO_CHARTS_FIRST_LINE_START"
AUTO_CHARTS_END = "AUTO_CHARTS_FIRST_LINE_END"


# =============================================================================
# NOTION HELPERS
# =============================================================================

def notion_headers(version=None):
    if not NOTION_TOKEN:
        raise RuntimeError(
            "Missing NOTION_TOKEN. Run:\n"
            'export NOTION_TOKEN="your_notion_token"'
        )

    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": version or NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method, path, version=None, **kwargs):
    response = requests.request(
        method,
        f"{BASE_URL}{path}",
        headers=notion_headers(version=version),
        timeout=90,
        **kwargs,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Notion API request failed: {method} {path}\n"
            f"Status: {response.status_code}\n"
            f"Response: {response.text}"
        )

    if not response.text:
        return {}

    return response.json()


def extract_id(value):
    value = value.strip()

    if value.startswith("http"):
        path = urlparse(value).path.strip("/")
        last_part = path.split("/")[-1]
        compact = last_part.replace("-", "")
        return compact[-32:]

    return value.replace("-", "")


def normalize_page_id(value):
    return extract_id(value)


def get_rich_text_plain_text(rich_text):
    return "".join(part.get("plain_text", "") for part in rich_text or []).strip()


def block_plain_text(block):
    block_type = block.get("type")
    block_data = block.get(block_type, {})

    if block_type in {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "toggle",
        "callout",
        "quote",
    }:
        return get_rich_text_plain_text(block_data.get("rich_text"))

    if block_type == "child_database":
        return block_data.get("title", "")

    if block_type == "child_page":
        return block_data.get("title", "")

    return ""


def list_block_children(block_id):
    children = []
    start_cursor = None

    while True:
        params = {"page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor

        result = notion_request(
            "GET",
            f"/blocks/{block_id}/children",
            params=params,
        )

        children.extend(result.get("results", []))

        if not result.get("has_more"):
            return children

        start_cursor = result.get("next_cursor")


def chunked(items, size):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def archive_block(block_id):
    return notion_request(
        "PATCH",
        f"/blocks/{block_id}",
        json={"archived": True},
    )


# =============================================================================
# DATABASE/TEMPLATE COPY
# =============================================================================

def next_week_iso():
    today = date.today()
    return (today + timedelta(days=7)).isoformat()


def get_reporting_week():
    return REPORTING_WEEK or next_week_iso()


def get_new_title():
    if NEW_WBR_TITLE:
        return NEW_WBR_TITLE
    return f"WBR - {get_reporting_week()}"


def get_page_title(page):
    for property_value in page.get("properties", {}).values():
        if property_value.get("type") == "title":
            return get_rich_text_plain_text(property_value.get("title"))
    return ""


def find_child_database_id(container_page_id):
    children = list_block_children(container_page_id)
    child_databases = [
        block for block in children
        if block.get("type") == "child_database"
    ]

    if not child_databases:
        raise RuntimeError(
            "No child_database block found on the provided page. "
            "Open the database as a full page or set NOTION_CHILD_DATABASE_ID directly."
        )

    print("Child databases found on page:")
    for index, block in enumerate(child_databases, start=1):
        title = block.get("child_database", {}).get("title", "")
        print(f"  {index}. {title!r} | {block['id']}")

    selected = child_databases[0]
    selected_title = selected.get("child_database", {}).get("title", "")
    print(f"Using child database: {selected_title!r} | {selected['id']}")
    return selected["id"]


def resolve_database_id(value):
    if NOTION_CHILD_DATABASE_ID:
        child_id = extract_id(NOTION_CHILD_DATABASE_ID)
        print(f"Using explicit NOTION_CHILD_DATABASE_ID: {child_id}")
        return child_id

    candidate_id = extract_id(value)

    try:
        notion_request("GET", f"/databases/{candidate_id}")
        print(f"Provided ID is a database: {candidate_id}")
        return candidate_id
    except RuntimeError as database_error:
        print("Provided ID is not a database. Treating it as a page that contains a database.")
        print(f"Database lookup error: {database_error}")

    notion_request("GET", f"/pages/{candidate_id}")
    print(f"Provided ID is a page. Searching for child_database blocks inside page: {candidate_id}")
    return extract_id(find_child_database_id(candidate_id))


def query_database_items(database_id):
    database_id = extract_id(database_id)

    results = []
    start_cursor = None

    while True:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        response = notion_request(
            "POST",
            f"/databases/{database_id}/query",
            json=payload,
        )

        results.extend(response.get("results", []))

        if not response.get("has_more"):
            return results

        start_cursor = response.get("next_cursor")


def find_template_item(database_id, template_title):
    if TEMPLATE_ITEM_PAGE_ID:
        page_id = extract_id(TEMPLATE_ITEM_PAGE_ID)
        print(f"Using explicit TEMPLATE_ITEM_PAGE_ID: {page_id}")
        return notion_request("GET", f"/pages/{page_id}")

    items = query_database_items(database_id)

    print("Database items found:")
    for item in items:
        title = get_page_title(item)
        print(f"  - {title!r} | {item['id']}")

    normalized_target = template_title.lower().strip()

    for item in items:
        title = get_page_title(item).lower().strip()
        if normalized_target in title:
            print(f"Matched template item: {get_page_title(item)!r} | {item['id']}")
            return item

    raise RuntimeError(
        f"Could not find a database item containing title {template_title!r}."
    )


def title_rich_text(title):
    return [{"type": "text", "text": {"content": title}}]


def safe_rich_text_for_write(rich_text):
    output = []

    for item in rich_text or []:
        item_type = item.get("type", "text")
        plain_text = item.get("plain_text", "")

        if item_type == "text":
            text_payload = item.get("text") or {"content": plain_text}
            content = text_payload.get("content", plain_text)
            safe_item = {
                "type": "text",
                "text": {"content": content},
            }
            if text_payload.get("link"):
                safe_item["text"]["link"] = text_payload["link"]

        elif item_type == "equation":
            expression = item.get("equation", {}).get("expression", plain_text)
            safe_item = {
                "type": "equation",
                "equation": {"expression": expression},
            }

        elif item_type == "mention":
            mention = item.get("mention", {})
            mention_type = mention.get("type")

            if mention_type == "date":
                date_obj = mention.get("date") or {}
                start = date_obj.get("start")
                if start and "YYYY" not in start and "MM" not in start and "DD" not in start:
                    safe_item = {
                        "type": "mention",
                        "mention": {"type": "date", "date": date_obj},
                    }
                else:
                    safe_item = {
                        "type": "text",
                        "text": {"content": plain_text or "YYYY-MM-DD"},
                    }

            elif mention_type in {"user", "page", "database"} and mention.get(mention_type, {}).get("id"):
                safe_item = {
                    "type": "mention",
                    "mention": {
                        "type": mention_type,
                        mention_type: {"id": mention[mention_type]["id"]},
                    },
                }
            else:
                safe_item = {
                    "type": "text",
                    "text": {"content": plain_text},
                }

        else:
            safe_item = {
                "type": "text",
                "text": {"content": plain_text},
            }

        annotations = item.get("annotations")
        if annotations:
            safe_item["annotations"] = annotations

        output.append(safe_item)

    return output


def clone_properties(source_properties, new_title, reporting_week):
    cloned = {}

    for property_name, property_value in source_properties.items():
        property_type = property_value.get("type")

        if property_type == "title":
            cloned[property_name] = {"title": title_rich_text(new_title)}

        elif property_type == "rich_text":
            cloned[property_name] = {
                "rich_text": safe_rich_text_for_write(property_value.get("rich_text"))
            }

        elif property_type == "number":
            cloned[property_name] = {"number": property_value.get("number")}

        elif property_type == "select":
            selected = property_value.get("select")
            cloned[property_name] = {
                "select": {"name": selected["name"]} if selected else None
            }

        elif property_type == "multi_select":
            cloned[property_name] = {
                "multi_select": [
                    {"name": item["name"]}
                    for item in property_value.get("multi_select", [])
                ]
            }

        elif property_type == "status":
            status = property_value.get("status")
            cloned[property_name] = {
                "status": {"name": status["name"]} if status else None
            }

        elif property_type == "date":
            if UPDATE_DATE_PROPERTIES and reporting_week and (
                "week" in property_name.lower()
                or "date" in property_name.lower()
                or "report" in property_name.lower()
            ):
                cloned[property_name] = {"date": {"start": reporting_week}}
            else:
                cloned[property_name] = {"date": property_value.get("date")}

        elif property_type == "checkbox":
            cloned[property_name] = {"checkbox": property_value.get("checkbox", False)}

        elif property_type == "url":
            cloned[property_name] = {"url": property_value.get("url")}

        elif property_type == "email":
            cloned[property_name] = {"email": property_value.get("email")}

        elif property_type == "phone_number":
            cloned[property_name] = {"phone_number": property_value.get("phone_number")}

        elif property_type == "people":
            cloned[property_name] = {
                "people": [
                    {"id": person["id"]}
                    for person in property_value.get("people", [])
                    if person.get("id")
                ]
            }

        elif property_type == "relation":
            cloned[property_name] = {
                "relation": [
                    {"id": relation["id"]}
                    for relation in property_value.get("relation", [])
                    if relation.get("id")
                ]
            }

        else:
            print(f"Skipping property {property_name!r} of type {property_type!r}")

    return remove_none_values(cloned)


def remove_none_values(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if item is None:
                continue
            cleaned[key] = remove_none_values(item)
        return cleaned

    if isinstance(value, list):
        return [remove_none_values(item) for item in value]

    return value


def safe_color(block_data):
    return block_data.get("color", "default")


def text_block_payload(block, block_type):
    source_data = block.get(block_type, {})
    data = {
        "rich_text": safe_rich_text_for_write(source_data.get("rich_text")),
        "color": safe_color(source_data),
    }

    if block_type in {"heading_1", "heading_2", "heading_3"}:
        if "is_toggleable" in source_data:
            data["is_toggleable"] = source_data.get("is_toggleable", False)

    if block_type == "to_do":
        data["checked"] = source_data.get("checked", False)

    if block_type == "callout" and isinstance(source_data.get("icon"), dict):
        data["icon"] = source_data["icon"]

    return {"type": block_type, block_type: remove_none_values(data)}


def table_row_to_create_payload(row_block):
    row_data = row_block.get("table_row", {})
    cells = [
        safe_rich_text_for_write(cell)
        for cell in row_data.get("cells", [])
    ]

    return {
        "type": "table_row",
        "table_row": {"cells": cells},
    }


def table_to_create_payload(block):
    table_data = block.get("table", {})
    source_rows = list_block_children(block["id"])

    row_payloads = [
        table_row_to_create_payload(row)
        for row in source_rows
        if row.get("type") == "table_row"
    ]

    table_width = table_data.get("table_width")

    if not table_width:
        if row_payloads:
            table_width = len(row_payloads[0]["table_row"].get("cells", []))
        else:
            table_width = 1

    if not row_payloads:
        row_payloads = [
            {
                "type": "table_row",
                "table_row": {
                    "cells": [
                        [{"type": "text", "text": {"content": ""}}]
                        for _ in range(table_width)
                    ]
                },
            }
        ]

    return {
        "type": "table",
        "table": {
            "table_width": table_width,
            "has_column_header": table_data.get("has_column_header", False),
            "has_row_header": table_data.get("has_row_header", False),
            "children": row_payloads,
        },
    }


def block_to_create_payload(block):
    block_type = block.get("type")

    if not block_type or block_type == "unsupported":
        print(f"Skipping unsupported block: {block.get('id')}")
        return None

    text_types = {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "toggle",
        "callout",
        "quote",
    }

    if block_type in text_types:
        return text_block_payload(block, block_type)

    if block_type == "divider":
        return {"type": "divider", "divider": {}}

    if block_type == "breadcrumb":
        return {"type": "breadcrumb", "breadcrumb": {}}

    if block_type == "table_of_contents":
        source_data = block.get("table_of_contents", {})
        return {
            "type": "table_of_contents",
            "table_of_contents": {"color": safe_color(source_data)},
        }

    if block_type == "column_list":
        return {"type": "column_list", "column_list": {}}

    if block_type == "column":
        return {"type": "column", "column": {}}

    if block_type == "synced_block":
        return {"type": "synced_block", "synced_block": {}}

    if block_type == "table":
        return table_to_create_payload(block)

    if block_type == "table_row":
        return None

    if block_type in {"child_database", "child_page", "link_preview"}:
        print(f"Skipping non-copyable block type {block_type}: {block.get('id')}")
        return None

    if block_type == "image":
        if SKIP_IMAGES:
            print(f"Skipping image block because SKIP_IMAGES=true: {block.get('id')}")
            return None

        image_data = block.get("image", {})
        if image_data.get("type") == "external":
            return {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": image_data.get("external"),
                    "caption": safe_rich_text_for_write(image_data.get("caption")),
                },
            }

        print(f"Skipping Notion-hosted image block: {block.get('id')}")
        return None

    if block_type in {"file", "pdf", "video", "audio", "embed", "bookmark", "equation", "code"}:
        print(f"Skipping file/embed/code-like block type {block_type}: {block.get('id')}")
        return None

    print(f"Skipping unhandled block type {block_type}: {block.get('id')}")
    return None


def append_children(parent_block_id, children_payloads):
    created = []

    for batch in chunked(children_payloads, 100):
        batch = [payload for payload in batch if payload]

        if not batch:
            continue

        result = notion_request(
            "PATCH",
            f"/blocks/{parent_block_id}/children",
            json={"children": batch},
        )
        created.extend(result.get("results", []))

    return created


def copy_block_children(source_parent_id, target_parent_id, depth=0):
    """Copy block children sequentially and recursively.

    Important: this skips previously generated automated chart sections from
    the source template, but it will not skip the rest of the report if an old
    generated section is missing its END marker. That was the cause of WBRs
    stopping at 17.7 instead of continuing through section 20.
    """
    if depth > MAX_COPY_DEPTH:
        print(f"Reached MAX_COPY_DEPTH={MAX_COPY_DEPTH}, stopping at {source_parent_id}")
        return {"created": 0, "skipped": 0, "failed": 0, "flattened": 0}

    source_children = list_block_children(source_parent_id)

    created_count = 0
    skipped_count = 0
    failed_count = 0
    flattened_count = 0

    skipping_auto_chart_section = False
    skipped_auto_chart_blocks = 0
    max_auto_chart_blocks = int(os.getenv("MAX_AUTO_CHART_BLOCKS", "25"))

    print(
        f"{'  ' * depth}Copying {len(source_children)} child blocks "
        f"from {source_parent_id} to {target_parent_id} at depth {depth}"
    )

    for index, source_block in enumerate(source_children, start=1):
        source_type = source_block.get("type")
        source_text = block_plain_text(source_block)
        has_children = bool(source_block.get("has_children"))

        if AUTO_CHARTS_START in source_text:
            skipping_auto_chart_section = True
            skipped_auto_chart_blocks = 1
            skipped_count += 1
            print(
                f"{'  ' * depth}Skipping old automated chart section from template "
                f"starting at block {index}/{len(source_children)}"
            )
            continue

        if skipping_auto_chart_section:
            # If the old auto chart section has no end marker, do not skip real
            # WBR headings such as 17.8, 18, 19, or 20. Resume copying and let
            # this same block be processed normally below.
            if is_heading_block(source_block) and looks_like_wbr_section_heading(source_text):
                skipping_auto_chart_section = False
                skipped_auto_chart_blocks = 0
                print(
                    f"{'  ' * depth}Detected real WBR section heading while skipping old charts: "
                    f"{source_text!r}. Resuming normal template copy."
                )
            else:
                skipped_count += 1
                skipped_auto_chart_blocks += 1

                if AUTO_CHARTS_END in source_text:
                    skipping_auto_chart_section = False
                    skipped_auto_chart_blocks = 0
                    print(
                        f"{'  ' * depth}Finished skipping old automated chart section "
                        f"at block {index}/{len(source_children)}"
                    )
                    continue

                if skipped_auto_chart_blocks >= max_auto_chart_blocks:
                    skipping_auto_chart_section = False
                    skipped_auto_chart_blocks = 0
                    print(
                        f"{'  ' * depth}WARNING: Old automated chart section had no end marker "
                        f"within {max_auto_chart_blocks} blocks. Resuming template copy so later "
                        "WBR sections are not lost."
                    )
                    continue

                continue

        payload = block_to_create_payload(source_block)

        if not payload:
            skipped_count += 1

            if has_children:
                print(
                    f"{'  ' * depth}Flattening children of skipped block "
                    f"{index}/{len(source_children)}: type={source_type} "
                    f"id={source_block.get('id')} text={source_text!r}"
                )
                child_result = copy_block_children(
                    source_parent_id=source_block["id"],
                    target_parent_id=target_parent_id,
                    depth=depth + 1,
                )
                created_count += child_result["created"]
                skipped_count += child_result["skipped"]
                failed_count += child_result["failed"]
                flattened_count += 1 + child_result.get("flattened", 0)

            continue

        try:
            created = append_children(target_parent_id, [payload])
        except RuntimeError as single_error:
            failed_count += 1
            print(
                f"{'  ' * depth}FAILED to copy block {index}/{len(source_children)}: "
                f"type={source_type} id={source_block.get('id')} text={source_text!r}"
            )
            print(f"{'  ' * depth}Error: {single_error}")

            if has_children:
                print(
                    f"{'  ' * depth}Recovering by flattening children of failed block "
                    f"type={source_type} id={source_block.get('id')}"
                )
                child_result = copy_block_children(
                    source_parent_id=source_block["id"],
                    target_parent_id=target_parent_id,
                    depth=depth + 1,
                )
                created_count += child_result["created"]
                skipped_count += child_result["skipped"]
                failed_count += child_result["failed"]
                flattened_count += 1 + child_result.get("flattened", 0)

            continue

        if not created:
            failed_count += 1
            print(
                f"{'  ' * depth}WARNING: Notion returned no created block for "
                f"type={source_type} id={source_block.get('id')}"
            )

            if has_children:
                print(
                    f"{'  ' * depth}Recovering by flattening children because no block was returned."
                )
                child_result = copy_block_children(
                    source_parent_id=source_block["id"],
                    target_parent_id=target_parent_id,
                    depth=depth + 1,
                )
                created_count += child_result["created"]
                skipped_count += child_result["skipped"]
                failed_count += child_result["failed"]
                flattened_count += 1 + child_result.get("flattened", 0)

            continue

        created_block = created[0]
        created_count += 1

        if source_type == "table":
            continue

        if has_children:
            child_result = copy_block_children(
                source_parent_id=source_block["id"],
                target_parent_id=created_block["id"],
                depth=depth + 1,
            )
            created_count += child_result["created"]
            skipped_count += child_result["skipped"]
            failed_count += child_result["failed"]
            flattened_count += child_result.get("flattened", 0)

    print(
        f"{'  ' * depth}Finished depth {depth}: "
        f"created={created_count}, skipped={skipped_count}, "
        f"failed={failed_count}, flattened={flattened_count}"
    )

    return {
        "created": created_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "flattened": flattened_count,
    }


def create_duplicate_item(source_item, new_title, reporting_week):
    parent = source_item.get("parent")
    if not parent:
        raise RuntimeError("Template item does not have a parent.")

    if parent.get("type") not in {"database_id", "data_source_id"}:
        raise RuntimeError(
            "The matched template item is not inside a database/data source. "
            f"Parent was: {parent}"
        )

    properties = clone_properties(
        source_properties=source_item.get("properties", {}),
        new_title=new_title,
        reporting_week=reporting_week,
    )

    payload = {"parent": parent, "properties": properties}
    return notion_request("POST", "/pages", json=payload)


def is_heading_block(block):
    return block.get("type") in {"heading_1", "heading_2", "heading_3"}


def looks_like_wbr_section_heading(text):
    """Return True for WBR section headings like 17.8, 18, 19.2, 20, etc."""
    cleaned = str(text or "").strip()
    return bool(re.match(r"^\d+(?:\.\d+)*\b", cleaned))


def collect_heading_texts_recursive(parent_block_id, depth=0, max_depth=None):
    """Collect heading texts from a page/block in Notion display order.

    Skips previously generated automated chart sections so tail recovery does
    not treat old automated chart headings as missing template content.
    """
    if max_depth is not None and depth > max_depth:
        return []

    headings = []
    skipping_auto_section = False

    for block in list_block_children(parent_block_id):
        text = block_plain_text(block)

        if AUTO_CHARTS_START in text:
            skipping_auto_section = True
            continue

        if skipping_auto_section:
            if AUTO_CHARTS_END in text:
                skipping_auto_section = False
            continue

        if is_heading_block(block) and text:
            headings.append(text)

        if block.get("has_children") and block.get("type") != "table":
            headings.extend(
                collect_heading_texts_recursive(
                    block["id"],
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            )

    return headings


def flatten_copy_blocks_from_marker(
    source_parent_id,
    target_parent_id,
    marker_heading_text,
    state=None,
    depth=0,
):
    """Append source content from marker heading onward into the target page.

    This is a recovery pass. If the structured Notion copy misses the tail of
    the WBR template, this flattens the remaining source blocks into the target
    page so that later sections are not lost.
    """
    if state is None:
        state = {
            "started": False,
            "created": 0,
            "skipped": 0,
            "failed": 0,
        }

    for source_block in list_block_children(source_parent_id):
        source_type = source_block.get("type")
        source_text = block_plain_text(source_block)

        if not state["started"]:
            if is_heading_block(source_block) and source_text == marker_heading_text:
                state["started"] = True
                print(
                    "Tail recovery started from missing heading: "
                    f"{marker_heading_text!r}"
                )
            else:
                if source_block.get("has_children") and source_type != "table":
                    flatten_copy_blocks_from_marker(
                        source_parent_id=source_block["id"],
                        target_parent_id=target_parent_id,
                        marker_heading_text=marker_heading_text,
                        state=state,
                        depth=depth + 1,
                    )
                continue

        # Once started, append every copyable block in source order.
        payload = block_to_create_payload(source_block)

        if not payload:
            state["skipped"] += 1
        else:
            try:
                append_children(target_parent_id, [payload])
                state["created"] += 1
            except RuntimeError as error:
                state["failed"] += 1
                print(
                    "Tail recovery failed to copy block: "
                    f"type={source_type} id={source_block.get('id')} "
                    f"text={source_text!r}"
                )
                print(f"Tail recovery error: {error}")

        if source_block.get("has_children") and source_type != "table":
            flatten_copy_blocks_from_marker(
                source_parent_id=source_block["id"],
                target_parent_id=target_parent_id,
                marker_heading_text=marker_heading_text,
                state=state,
                depth=depth + 1,
            )

    return state


def recover_missing_template_tail(source_page_id, target_page_id):
    """If copied page is missing later WBR headings, append the missing tail.

    This is designed for the current issue where the copy reaches 17.7 and
    the automated graphs are inserted correctly, but sections after 17.7 are
    missing. We compare source and target heading lists. If a source heading is
    missing from the target, we append the source template from that heading
    onward using a flattened recovery copy.
    """
    print("Checking copied WBR for missing template headings...")

    source_headings = collect_heading_texts_recursive(source_page_id)
    target_headings = collect_heading_texts_recursive(target_page_id)

    target_heading_set = set(target_headings)

    print(f"Source headings found: {len(source_headings)}")
    print(f"Target headings found: {len(target_headings)}")

    first_missing_heading = None

    for heading in source_headings:
        # Do not use old automated chart headings as a tail-recovery marker.
        # The copy step intentionally skips old generated chart sections, so
        # those headings are expected to be missing in the new WBR.
        if "Automated First Line" in heading or AUTO_CHARTS_START in heading or AUTO_CHARTS_END in heading:
            continue

        if heading not in target_heading_set:
            first_missing_heading = heading
            break

    if not first_missing_heading:
        print("No missing headings detected after copy.")
        return {
            "recovered": False,
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "first_missing_heading": None,
        }

    old_chart_headings = {
        "1 Week View",
        "Inbound",
        "Outbound",
        "Chats",
    }

    if first_missing_heading in old_chart_headings:
        print(
            f"Skipping tail recovery because first missing heading {first_missing_heading!r} "
            "looks like an old automated chart section."
        )
        return {
            "recovered": False,
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "first_missing_heading": first_missing_heading,
        }

    print(f"First missing heading detected: {first_missing_heading!r}")
    print("Appending missing template tail using flattened recovery copy...")

    result = flatten_copy_blocks_from_marker(
        source_parent_id=source_page_id,
        target_parent_id=target_page_id,
        marker_heading_text=first_missing_heading,
    )

    print(
        "Tail recovery complete. "
        f"created={result['created']}, "
        f"skipped={result['skipped']}, "
        f"failed={result['failed']}"
    )

    return {
        "recovered": True,
        "created": result["created"],
        "skipped": result["skipped"],
        "failed": result["failed"],
        "first_missing_heading": first_missing_heading,
    }


def create_wbr_item():
    database_id = resolve_database_id(NOTION_DATABASE_ID)
    reporting_week = get_reporting_week()
    new_title = get_new_title()

    print("=" * 100)
    print("STEP 1: COPY WBR TEMPLATE DATABASE ITEM")
    print("=" * 100)
    print(f"Database ID: {database_id}")
    print(f"Template item title contains: {TEMPLATE_ITEM_TITLE!r}")
    print(f"Reporting week: {reporting_week}")
    print(f"New title: {new_title}")
    print(f"COPY_BLOCKS={COPY_BLOCKS}")
    print(f"SKIP_IMAGES={SKIP_IMAGES}")

    template_item = find_template_item(
        database_id=database_id,
        template_title=TEMPLATE_ITEM_TITLE,
    )

    template_item_id = template_item["id"]
    print(f"Template item/page ID: {template_item_id}")

    new_item = create_duplicate_item(
        source_item=template_item,
        new_title=new_title,
        reporting_week=reporting_week,
    )

    new_item_id = new_item["id"]
    print(f"Created new WBR database item/page: {new_item_id}")

    if COPY_BLOCKS:
        print("Copying supported page blocks from template item...")
        copy_result = copy_block_children(
            source_parent_id=template_item_id,
            target_parent_id=new_item_id,
        )
        print(
            "Block copy complete. "
            f"created={copy_result['created']}, "
            f"skipped={copy_result['skipped']}, "
            f"failed={copy_result['failed']}, "
            f"flattened={copy_result.get('flattened', 0)}"
        )

        # Safety pass: make sure later template sections were not lost.
        recover_missing_template_tail(
            source_page_id=template_item_id,
            target_page_id=new_item_id,
        )
    else:
        print("COPY_BLOCKS=false, skipping page body copy.")

    print()
    print("COPY SUCCESS")
    print(f"NEW_NOTION_PAGE_ID={new_item_id}")
    print(f"NEW_NOTION_PAGE_URL={new_item.get('url')}")

    return new_item_id, new_item.get("url")


# =============================================================================
# CSV + CHART GENERATION
# =============================================================================

def load_font(size, bold=False):
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    ]

    for font_path in font_candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size=size)

    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def draw_centered_text(draw, xy, text, font, fill):
    x, y = xy
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (right - left) / 2, y - (bottom - top) / 2), text, font=font, fill=fill)


def draw_rotated_text(base_image, xy, text, font, fill, angle):
    x, y = xy
    dummy = Image.new("RGBA", (10, 10), (255, 255, 255, 0))
    dummy_draw = ImageDraw.Draw(dummy)
    bbox = dummy_draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    text_image = Image.new("RGBA", (text_width + 20, text_height + 20), (255, 255, 255, 0))
    text_draw = ImageDraw.Draw(text_image)
    text_draw.text((10, 10), text, font=font, fill=fill)

    rotated = text_image.rotate(angle, expand=True)
    base_image.alpha_composite(
        rotated,
        (int(x - rotated.width / 2), int(y - rotated.height / 2)),
    )


def nice_axis_max(value):
    if value <= 5:
        return 5
    if value <= 10:
        return 10
    if value <= 25:
        return 25
    if value <= 50:
        return 50
    if value <= 100:
        return 100
    if value <= 500:
        return math.ceil(value / 100) * 100
    if value <= 2000:
        return math.ceil(value / 250) * 250
    return math.ceil(value / 500) * 500


def parse_number(value):
    if value is None:
        return None

    cleaned = str(value).strip().replace(",", "")
    if cleaned == "":
        return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_percent(value):
    if value is None:
        return None

    cleaned = str(value).strip().replace(",", "")
    if cleaned == "":
        return None

    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]

    try:
        return float(cleaned) / 100
    except ValueError:
        return None


def clean_value(value):
    return "" if value is None else str(value).strip()



def read_metrics_csv(csv_path):
    """Read a Looker CSV and normalize its date column to DATE_FIELD.

    The previous version only worked when DATE_FIELD appeared exactly in the
    first five rows. This version:
    - prints a small preview for debugging in GitHub Actions logs,
    - finds the header row more defensively,
    - accepts common date-field variants,
    - aliases the detected date field back to DATE_FIELD so the chart code can
      keep using one canonical name.
    """
    csv_path = Path(csv_path)

    with csv_path.open(newline="", encoding="utf-8-sig") as file_handle:
        raw_rows = list(csv.reader(file_handle))

    if not raw_rows:
        raise RuntimeError(f"CSV is empty: {csv_path}")

    print(f"Reading CSV: {csv_path}")
    print(f"CSV size bytes: {csv_path.stat().st_size}")
    print("CSV preview first 5 rows:")
    for preview_row in raw_rows[:5]:
        print(f"  {preview_row}")

    possible_date_fields = [
        DATE_FIELD,
        "Interaction Date",
        "Date",
        "Created Date",
        "Conversation Date",
        "Chat Date",
        "Call Date",
        "Day",
        "Week",
    ]

    def normalized(value):
        return clean_value(value).lower()

    possible_date_fields_lower = {field.lower() for field in possible_date_fields}

    header_index = None
    detected_date_field = None

    # Prefer a header row with the canonical DATE_FIELD or a common equivalent.
    for index, row in enumerate(raw_rows[:20]):
        normalized_row = [normalized(cell) for cell in row]
        for cell in row:
            if normalized(cell) in possible_date_fields_lower:
                header_index = index
                detected_date_field = clean_value(cell)
                break
        if header_index is not None:
            break

    # Fallback: find a header row containing any date-like column name.
    if header_index is None:
        for index, row in enumerate(raw_rows[:20]):
            for cell in row:
                cell_lower = normalized(cell)
                if "date" in cell_lower or cell_lower in {"day", "week"}:
                    header_index = index
                    detected_date_field = clean_value(cell)
                    break
            if header_index is not None:
                break

    if header_index is None:
        raise RuntimeError(
            f"Could not detect a header row in CSV: {csv_path}\n"
            f"Expected a date-like column such as {DATE_FIELD!r}.\n"
            f"First 5 raw rows: {raw_rows[:5]}"
        )

    headers = raw_rows[header_index]
    headers = [
        header if clean_value(header) else "__row_number"
        for header in headers
    ]

    if detected_date_field not in headers:
        # This should rarely happen, but avoids a confusing downstream error.
        detected_date_field = None
        for header in headers:
            header_lower = header.lower()
            if header_lower in possible_date_fields_lower or "date" in header_lower:
                detected_date_field = header
                break

    if not detected_date_field:
        raise RuntimeError(
            f"Could not detect date column in CSV: {csv_path}\n"
            f"Detected headers: {headers}\n"
            f"First 5 raw rows: {raw_rows[:5]}"
        )

    data_rows = []
    for raw_row in raw_rows[header_index + 1:]:
        if not any(clean_value(cell) for cell in raw_row):
            continue

        padded = raw_row + [""] * (len(headers) - len(raw_row))
        row = {
            header: clean_value(value)
            for header, value in zip(headers, padded)
        }

        date_value = row.get(detected_date_field)
        if date_value:
            row[DATE_FIELD] = date_value
            data_rows.append(row)

    if not data_rows:
        raise RuntimeError(
            f"No data rows found in CSV: {csv_path}\n"
            f"Detected header row index: {header_index}\n"
            f"Detected date field: {detected_date_field!r}\n"
            f"Detected headers: {headers}\n"
            f"First 10 raw rows: {raw_rows[:10]}"
        )

    return sorted(data_rows, key=lambda row: row[DATE_FIELD])



def find_csv_by_report_slug(report_slug, explicit_path=None):
    """Find one CSV for an exact report slug in CSV_DIR.

    Filenames look like:
      first-line-inbound-8-week-2026-06-08T17-33-20-169Z-qoc4wc.csv

    We intentionally match by slug prefix instead of loose keywords because the
    date in the filename contains "8", which made the old keyword matcher choose
    first-line-inbound-weekly for the inbound 8-week report.
    """
    if explicit_path:
        path = Path(explicit_path)
        if path.exists():
            return path
        raise FileNotFoundError(f"Explicit CSV path does not exist: {path}")

    search_root = CSV_DIR

    if not search_root.exists():
        raise FileNotFoundError(f"CSV_DIR does not exist: {search_root}")

    report_slug = report_slug.lower().strip()
    candidates = []

    for path in search_root.glob("*.csv"):
        name = path.name.lower()
        stem = path.stem.lower()

        if stem == report_slug or name.startswith(f"{report_slug}-"):
            candidates.append(path)

    if not candidates:
        available = "\n".join(sorted(p.name for p in search_root.glob("*.csv")))
        raise FileNotFoundError(
            f"Could not find CSV for report slug: {report_slug}\n"
            f"CSV_DIR: {search_root}\n"
            f"Available CSVs:\n{available}"
        )

    selected = max(candidates, key=lambda path: path.stat().st_mtime)
    print(f"Selected CSV for {report_slug}: {selected}")
    return selected


def find_csv_by_keywords(keywords, explicit_path=None):
    """Backward-compatible wrapper.

    New automation code should use find_csv_by_report_slug. This fallback remains
    for older local runs, but exact report slug matching is safer.
    """
    if explicit_path:
        path = Path(explicit_path)
        if path.exists():
            return path
        raise FileNotFoundError(f"Explicit CSV path does not exist: {path}")

    search_roots = [CSV_DIR, Path("."), Path("/mnt/data")]
    candidates = []

    for root in search_roots:
        if not root.exists():
            continue

        for path in root.rglob("*.csv"):
            name = path.name.lower()
            if all(keyword.lower() in name for keyword in keywords):
                candidates.append(path)

    if not candidates:
        raise FileNotFoundError(
            "Could not find CSV matching keywords: "
            f"{keywords}. Set an explicit env var path instead."
        )

    return max(candidates, key=lambda path: path.stat().st_mtime)


def format_count(value):
    return f"{value:,.0f}"


def format_percent(value):
    return f"{value * 100:.1f}%"


def render_combined_call_chart(
    rows,
    title,
    output_path,
    include_abandoned=True,
    fill_missing_percent_as_zero=False,
):
    dates = [row[DATE_FIELD] for row in rows]
    eligible_calls = [parse_number(row.get(ELIGIBLE_CALLS_FIELD)) or 0 for row in rows]

    def percent_series(field_name):
        values = []
        for row in rows:
            parsed = parse_percent(row.get(field_name))
            if parsed is None and fill_missing_percent_as_zero:
                parsed = 0
            values.append(parsed)
        return values

    ai_csat = percent_series(AI_CSAT_FIELD)
    participation = percent_series(AI_CSAT_PARTICIPATION_FIELD)

    if include_abandoned:
        abandoned = percent_series(ABANDONED_FIELD)
        abandoned_label = "Abandoned Call Rate [%]"
    else:
        abandoned = [0 for _ in rows]
        abandoned_label = "Missed Call Rate [%]"

    scale = 2
    width, height = 1750 * scale, 980 * scale
    plot_left = 215 * scale
    plot_right = 1505 * scale
    plot_top = 140 * scale
    plot_bottom = 670 * scale
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)

    font_title = load_font(38 * scale, bold=True)
    font_axis = load_font(24 * scale, bold=True)
    font_tick = load_font(22 * scale)
    font_label = load_font(21 * scale, bold=True)
    font_legend = load_font(21 * scale)

    colors = {
        "text": (44, 52, 62, 255),
        "muted": (107, 114, 128, 255),
        "grid": (224, 226, 228, 255),
        "eligible": (94, 196, 222, 255),
        "csat": (255, 137, 110, 255),
        "participation": (255, 194, 110, 255),
        "abandoned": (111, 154, 148, 255),
        "axis": (164, 170, 179, 255),
        "label_bg": (255, 255, 255, 225),
    }

    draw_centered_text(draw, (width / 2, 58 * scale), title, font_title, colors["text"])

    left_axis_max = nice_axis_max(max(eligible_calls) * 1.16 if eligible_calls else 1)

    percent_values = [
        value
        for value in [*ai_csat, *participation, *abandoned]
        if value is not None
    ]
    max_percent = max(percent_values) if percent_values else 1.0
    right_axis_max = 1.0 if max_percent <= 1.0 else min(1.15, math.ceil(max_percent * 10) / 10)

    side_padding = 78 * scale

    def x_at(index):
        if len(dates) == 1:
            return (plot_left + plot_right) / 2
        usable_width = plot_width - (2 * side_padding)
        return plot_left + side_padding + (usable_width * index / (len(dates) - 1))

    def y_left(value):
        return plot_bottom - (value / left_axis_max) * plot_height

    def y_right(value):
        return plot_bottom - (value / right_axis_max) * plot_height

    def draw_value_label(text, center_x, center_y, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        label_x = center_x - text_width / 2
        label_y = center_y - text_height / 2

        label_x = max(plot_left + 4 * scale, min(plot_right - text_width - 4 * scale, label_x))
        label_y = max(plot_top + 4 * scale, min(plot_bottom - text_height - 4 * scale, label_y))

        draw.rounded_rectangle(
            (
                label_x - 4 * scale,
                label_y - 3 * scale,
                label_x + text_width + 4 * scale,
                label_y + text_height + 5 * scale,
            ),
            radius=4 * scale,
            fill=colors["label_bg"],
        )
        draw.text((label_x, label_y), text, font=font, fill=fill)

    for index in range(6):
        tick = left_axis_max * index / 5
        y = y_left(tick)
        draw.line((plot_left, y, plot_right, y), fill=colors["grid"], width=2 * scale)

        label = format_count(tick)
        bbox = draw.textbbox((0, 0), label, font=font_tick)
        draw.text(
            (plot_left - 24 * scale - (bbox[2] - bbox[0]), y - 13 * scale),
            label,
            font=font_tick,
            fill=colors["text"],
        )

    for index in range(6):
        pct = right_axis_max * index / 5
        y = y_right(pct)
        label = f"{pct * 100:.0f}%"
        draw.text(
            (plot_right + 24 * scale, y - 13 * scale),
            label,
            font=font_tick,
            fill=colors["text"],
        )

    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=colors["axis"], width=2 * scale)
    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=colors["axis"], width=2 * scale)
    draw.line((plot_right, plot_top, plot_right, plot_bottom), fill=colors["axis"], width=2 * scale)

    slot = (plot_width - 2 * side_padding) / max(len(dates) - 1, 1)
    bar_width = min(92 * scale, slot * 0.58)

    for index, value in enumerate(eligible_calls):
        x = x_at(index)
        y = y_left(value)

        draw.rounded_rectangle(
            (x - bar_width / 2, y, x + bar_width / 2, plot_bottom),
            radius=4 * scale,
            fill=colors["eligible"],
        )

        draw_value_label(
            format_count(value),
            x,
            y - 18 * scale,
            font_label,
            colors["eligible"],
        )

    def draw_percent_line(values, color, label_position_offset=0):
        points = []
        for index, value in enumerate(values):
            if value is None:
                continue
            points.append((x_at(index), y_right(value), value, index))

        if len(points) >= 2:
            draw.line([(x, y) for x, y, _, _ in points], fill=color, width=4 * scale)

        for x, y, value, index in points:
            draw.ellipse(
                (x - 7 * scale, y - 7 * scale, x + 7 * scale, y + 7 * scale),
                fill=color,
            )

            draw_value_label(
                format_percent(value),
                x,
                y - (26 + label_position_offset) * scale,
                font_label,
                color,
            )

    draw_percent_line(abandoned, colors["abandoned"], label_position_offset=-8)
    draw_percent_line(ai_csat, colors["csat"], label_position_offset=16)
    draw_percent_line(participation, colors["participation"], label_position_offset=40)

    for index, date_label in enumerate(dates):
        x = x_at(index)
        display_date = date_label[5:] if len(date_label) >= 10 else date_label
        bbox = draw.textbbox((0, 0), display_date, font=font_tick)
        draw.text(
            (x - (bbox[2] - bbox[0]) / 2, plot_bottom + 22 * scale),
            display_date,
            font=font_tick,
            fill=colors["text"],
        )

    draw_centered_text(
        draw,
        ((plot_left + plot_right) / 2, plot_bottom + 72 * scale),
        "Interaction Date Dynamic",
        font_axis,
        colors["text"],
    )

    draw_rotated_text(
        image,
        (58 * scale, (plot_top + plot_bottom) / 2),
        "Eligible Calls [#]",
        font_axis,
        colors["eligible"],
        90,
    )
    draw_rotated_text(
        image,
        (1680 * scale, (plot_top + plot_bottom) / 2),
        "[%]",
        font_axis,
        colors["text"],
        90,
    )

    legend_items = [
        ("Eligible Calls [#]", colors["eligible"]),
        (abandoned_label, colors["abandoned"]),
        ("AI CSAT Call [%]", colors["csat"]),
        ("AI CSAT Call Participation Rate [%]", colors["participation"]),
    ]

    legend_y = 850 * scale
    legend_total_width = 0
    for text, _ in legend_items:
        bbox = draw.textbbox((0, 0), text, font=font_legend)
        legend_total_width += 38 * scale + (bbox[2] - bbox[0]) + 32 * scale

    legend_x = max(60 * scale, (width - legend_total_width) / 2)

    for text, color in legend_items:
        draw.ellipse(
            (
                legend_x,
                legend_y - 8 * scale,
                legend_x + 16 * scale,
                legend_y + 8 * scale,
            ),
            fill=color,
        )
        draw.text((legend_x + 24 * scale, legend_y - 13 * scale), text, font=font_legend, fill=colors["text"])
        bbox = draw.textbbox((0, 0), text, font=font_legend)
        legend_x += 38 * scale + (bbox[2] - bbox[0]) + 32 * scale

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = image.convert("RGB").resize((width // scale, height // scale), Image.Resampling.LANCZOS)
    image.save(output_path, "PNG", optimize=True)

    return output_path


def latest_date_from_rows(rows):
    sorted_rows = sorted(rows, key=lambda row: row[DATE_FIELD])
    return sorted_rows[-1][DATE_FIELD]


# =============================================================================
# CHART INSERTION
# =============================================================================

def upload_file_to_notion(file_path):
    # File uploads require the newer Notion API version.
    upload = notion_request(
        "POST",
        "/file_uploads",
        version="2026-03-11",
        json={},
    )

    upload_headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2026-03-11",
    }

    with file_path.open("rb") as file_handle:
        response = requests.post(
            upload["upload_url"],
            headers=upload_headers,
            files={"file": (file_path.name, file_handle, "image/png")},
            timeout=120,
        )

    if response.status_code >= 400:
        raise RuntimeError(
            "Notion file upload failed:\n"
            f"Status: {response.status_code}\n"
            f"Response: {response.text}"
        )

    return response.json()


def cleanup_existing_first_line_auto_charts(page_id):
    """Archive previous automated chart blocks safely, including nested blocks.

    Safety rule:
    - Archive sibling blocks from any AUTO_CHARTS_*_START marker through the
      matching AUTO_CHARTS_*_END marker.
    - If an end marker is missing, stop after MAX_AUTO_CHART_BLOCKS for that
      sibling group so we do not accidentally archive later WBR sections.
    """
    max_auto_chart_blocks = int(os.getenv("MAX_AUTO_CHART_BLOCKS", "25"))
    archived_count = 0

    def cleanup_parent(parent_id, depth=0):
        nonlocal archived_count
        children = list_block_children(parent_id)
        inside_auto_section = False
        archived_in_current_section = 0

        for block in children:
            text = block_plain_text(block)

            if AUTO_CHARTS_START in text:
                inside_auto_section = True
                archived_in_current_section = 0
                archive_block(block["id"])
                archived_count += 1
                archived_in_current_section += 1
                continue

            if inside_auto_section:
                archive_block(block["id"])
                archived_count += 1
                archived_in_current_section += 1

                if AUTO_CHARTS_END in text:
                    inside_auto_section = False
                    archived_in_current_section = 0
                    continue

                if archived_in_current_section >= max_auto_chart_blocks:
                    print(
                        "WARNING: Hit MAX_AUTO_CHART_BLOCKS while cleaning chart section. "
                        "Stopping cleanup for this sibling group to avoid archiving later WBR sections."
                    )
                    inside_auto_section = False
                    archived_in_current_section = 0
                    continue

            if not inside_auto_section and block.get("has_children") and block.get("type") != "table":
                cleanup_parent(block["id"], depth=depth + 1)

    cleanup_parent(page_id)

    if archived_count:
        print(f"Archived {archived_count} previous automated chart blocks.")

    return archived_count


def heading_level(block):
    block_type = block.get("type")
    if block_type == "heading_1":
        return 1
    if block_type == "heading_2":
        return 2
    if block_type == "heading_3":
        return 3
    return None


def flatten_blocks_recursive(parent_block_id, depth=0, max_depth=None):
    if max_depth is not None and depth > max_depth:
        return []

    flattened = []
    for block in list_block_children(parent_block_id):
        flattened.append(block)
        if block.get("has_children") and block.get("type") != "table":
            flattened.extend(
                flatten_blocks_recursive(
                    block["id"],
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            )
    return flattened


def normalize_anchor_text(value):
    return " ".join(str(value or "").strip().lower().split())


def is_auto_chart_block_text(text):
    text = text or ""
    return AUTO_CHARTS_START in text or AUTO_CHARTS_END in text


def find_block_after_heading(page_id, heading_contains, target_text):
    """Find an anchor inside a heading section, ignoring generated chart blocks.

    Rules:
    - Start inside the section whose heading contains `heading_contains`.
    - Stop when the next heading of the same or higher level is reached.
    - Ignore AUTO_CHARTS marker blocks and generated chart headings.
    - Prefer exact text match, then fallback to contains match.
    """
    blocks = flatten_blocks_recursive(page_id, max_depth=MAX_COPY_DEPTH + 3)

    inside_target_section = False
    target_heading_level = None
    target_norm = normalize_anchor_text(target_text)

    exact_match = None
    contains_match = None

    for block in blocks:
        text = block_plain_text(block)
        text_norm = normalize_anchor_text(text)
        level = heading_level(block)

        # Critical fix: never match generated chart markers as anchors.
        if is_auto_chart_block_text(text):
            continue

        if level is not None:
            if inside_target_section and target_heading_level is not None and level <= target_heading_level:
                break

            if heading_contains.lower() in text.lower():
                inside_target_section = True
                target_heading_level = level
                print(f"Found target heading: {text!r}")
                print(f"Heading block ID: {block['id']}")
                continue

        if not inside_target_section:
            continue

        if not text_norm:
            continue

        # Exact match is safest: "Outbound" should not match generated text.
        if text_norm == target_norm:
            exact_match = block
            break

        # Fallback contains match, but still never generated marker text.
        if target_norm in text_norm and not is_auto_chart_block_text(text):
            if contains_match is None:
                contains_match = block

    selected = exact_match or contains_match

    if selected:
        print(f"Found anchor block for {target_text!r}: {block_plain_text(selected)!r}")
        print(f"Anchor block ID: {selected['id']}")
        return selected

    return None


def get_append_parent_id(page_id, after_block):
    parent = after_block.get("parent", {})
    parent_type = parent.get("type")

    if parent_type == "page_id":
        return parent.get("page_id") or page_id
    if parent_type == "block_id":
        return parent.get("block_id") or page_id

    return page_id


def image_block_from_uploaded_chart(chart):
    return {
        "type": "image",
        "image": {
            "type": "file_upload",
            "file_upload": {"id": chart["file_upload_id"]},
            "caption": [
                {
                    "type": "text",
                    "text": {"content": chart["caption"]},
                }
            ],
        },
    }


def append_single_first_line_chart(page_id, after_block, chart, review_week):
    parent_id = get_append_parent_id(page_id, after_block)
    start_marker = f"{AUTO_CHARTS_START}_{chart['key']}"
    end_marker = f"{AUTO_CHARTS_END}_{chart['key']}"

    children = [
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": start_marker}}
                ]
            },
        },
        {
            "type": "heading_3",
            "heading_3": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"{chart['title']} — {review_week}"},
                    }
                ]
            },
        },
        image_block_from_uploaded_chart(chart),
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": end_marker}}
                ]
            },
        },
    ]

    return notion_request(
        "PATCH",
        f"/blocks/{parent_id}/children",
        json={"after": after_block["id"], "children": children},
    )


def build_chart_blocks(chart, review_week):
    """Build only visible chart blocks.

    We intentionally do not add AUTO_CHARTS marker paragraphs. The user-facing
    WBR should only show the chart heading and chart image.
    """
    return [
        {
            "type": "heading_3",
            "heading_3": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"{chart['title']} — {review_week}"},
                    }
                ]
            },
        },
        image_block_from_uploaded_chart(chart),
    ]


def append_chart_group_after_anchor(page_id, after_block, charts, review_week):
    """Append all charts after one anchor in the intended order.

    This is used when the copied template has only one First Line Support anchor
    such as 'New Daily 1 Week View' and does not contain separate Inbound,
    Outbound, and Chats anchors.
    """
    parent_id = get_append_parent_id(page_id, after_block)

    children = []
    for chart in charts:
        children.extend(build_chart_blocks(chart, review_week))

    return notion_request(
        "PATCH",
        f"/blocks/{parent_id}/children",
        json={"after": after_block["id"], "children": children},
    )



def heading_candidate_texts(heading_contains):
    value = str(heading_contains or "").strip()
    candidates = [value]

    # Robust fallback for the First Line Chats section. Some templates do not
    # include the exact "17.8" string in copied text, but do include "Chats" or
    # "First Line" + "Chat".
    if value == TARGET_CHATS_HEADING or "17.8" in value or "chat" in value.lower():
        candidates.extend([
            "17.8",
            "First Line Chats",
            "First Line Chat",
            "Chats",
            "Chat",
        ])

    # De-duplicate while preserving order.
    result = []
    seen = set()
    for item in candidates:
        key = item.lower()
        if item and key not in seen:
            result.append(item)
            seen.add(key)

    return result


def find_heading_block(page_id, heading_contains):
    """Find a heading block by text, including nested blocks."""
    blocks = flatten_blocks_recursive(page_id, max_depth=MAX_COPY_DEPTH + 3)
    candidates = heading_candidate_texts(heading_contains)

    for candidate in candidates:
        needle = str(candidate or "").lower().strip()

        for block in blocks:
            text = block_plain_text(block)
            text_lower = text.lower()

            if heading_level(block) is None:
                continue

            if needle in text_lower:
                print(f"Found heading for {heading_contains!r} using candidate {candidate!r}: {text!r}")
                print(f"Heading block ID: {block['id']}")
                return block

            # Extra-safe semantic match for chat headings.
            if (
                candidate.lower() in {"chats", "chat", "first line chats", "first line chat"}
                and "chat" in text_lower
                and ("first line" in text_lower or looks_like_wbr_section_heading(text))
            ):
                print(f"Found chat-like heading for {heading_contains!r}: {text!r}")
                print(f"Heading block ID: {block['id']}")
                return block

    return None


def append_chart_group_after_heading(page_id, heading_block, charts, review_week):
    """Append charts immediately after a heading block."""
    parent_id = get_append_parent_id(page_id, heading_block)

    children = []
    for chart in charts:
        children.extend(build_chart_blocks(chart, review_week))

    return notion_request(
        "PATCH",
        f"/blocks/{parent_id}/children",
        json={"after": heading_block["id"], "children": children},
    )


def first_available_anchor(page_id, heading_contains, chart_specs):
    """Find the first available anchor for a group of chart specs."""
    anchors_by_key = {}
    missing_specs = []

    for spec in chart_specs:
        anchor_block = find_block_after_heading(
            page_id=page_id,
            heading_contains=heading_contains,
            target_text=spec["anchor_text"],
        )

        if anchor_block:
            anchors_by_key[spec["key"]] = anchor_block
        else:
            missing_specs.append(spec)

    preferred = anchors_by_key.get(chart_specs[0]["key"])
    fallback = preferred or (next(iter(anchors_by_key.values())) if anchors_by_key else None)

    return fallback, anchors_by_key, missing_specs


def append_chart_group_to_section(page_id, heading_contains, chart_specs, uploaded_by_key, review_week):
    """Insert a group of charts into one WBR section.

    If the section has a matching anchor, insert after that anchor. If not,
    insert after the section heading itself.
    """
    charts_in_order = [uploaded_by_key[spec["key"]] for spec in chart_specs]

    fallback_anchor, anchors_by_key, missing_specs = first_available_anchor(
        page_id=page_id,
        heading_contains=heading_contains,
        chart_specs=chart_specs,
    )

    if fallback_anchor:
        if missing_specs:
            print(
                f"Some anchors under {heading_contains!r} were not found: "
                + ", ".join(spec["anchor_text"] for spec in missing_specs)
            )
            print(
                "Inserting this chart group after the first available anchor: "
                f"{block_plain_text(fallback_anchor)!r}"
            )

        append_chart_group_after_anchor(
            page_id=page_id,
            after_block=fallback_anchor,
            charts=charts_in_order,
            review_week=review_week,
        )
        print(
            f"Inserted {len(charts_in_order)} chart(s) under {heading_contains!r} "
            f"after anchor {block_plain_text(fallback_anchor)!r}."
        )
        return

    heading_block = find_heading_block(page_id, heading_contains)
    if heading_block:
        print(
            f"No anchors found under {heading_contains!r}. "
            "Inserting chart group directly after the section heading."
        )
        append_chart_group_after_heading(
            page_id=page_id,
            heading_block=heading_block,
            charts=charts_in_order,
            review_week=review_week,
        )
        print(
            f"Inserted {len(charts_in_order)} chart(s) directly after heading "
            f"{block_plain_text(heading_block)!r}."
        )
        return

    # Last-resort fallback: if the chat section is genuinely missing from the
    # copied template, create a visible heading at the end of the page and place
    # the chart there rather than failing after the WBR page has already been
    # created.
    if heading_contains == TARGET_CHATS_HEADING or "17.8" in str(heading_contains):
        print(
            f"Could not find {heading_contains!r}. Creating a new chat section at the end of the WBR."
        )
        created = append_children(
            page_id,
            [
                {
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "17.8 First Line Chats"},
                            }
                        ]
                    },
                }
            ],
        )
        if not created:
            raise RuntimeError("Failed to create fallback 17.8 First Line Chats heading.")

        append_chart_group_after_heading(
            page_id=page_id,
            heading_block=created[0],
            charts=charts_in_order,
            review_week=review_week,
        )
        print("Inserted chat chart under newly created '17.8 First Line Chats' heading.")
        return

    raise RuntimeError(
        f"Could not find section heading or anchor for {heading_contains!r}. "
        "Update TARGET_HEADING / TARGET_CHATS_HEADING or the relevant TARGET_*_ANCHOR_TEXT env var."
    )


def append_first_line_chart_set(page_id, chart_specs, uploaded_charts, review_week):
    uploaded_by_key = {chart["key"]: chart for chart in uploaded_charts}

    call_chart_specs = [
        spec for spec in chart_specs
        if spec["key"] in {"inbound_weekly", "inbound_8_week", "outbound_8_week"}
    ]
    chat_chart_specs = [
        spec for spec in chart_specs
        if spec["key"] in {"agent_chats", "bot_chats"}
    ]

    # 17.7 is First Line Calls. Insert the call charts there.
    append_chart_group_to_section(
        page_id=page_id,
        heading_contains=TARGET_HEADING,
        chart_specs=call_chart_specs,
        uploaded_by_key=uploaded_by_key,
        review_week=review_week,
    )

    # 17.8 is First Line Chats. Insert the chat chart there.
    append_chart_group_to_section(
        page_id=page_id,
        heading_contains=TARGET_CHATS_HEADING,
        chart_specs=chat_chart_specs,
        uploaded_by_key=uploaded_by_key,
        review_week=review_week,
    )


def available_csv_fields(rows):
    fields = []
    seen = set()
    for row in rows:
        for field in row.keys():
            if field not in seen:
                seen.add(field)
                fields.append(field)
    return fields


def find_field(rows, exact_candidates=None, keyword_groups=None):
    fields = available_csv_fields(rows)
    lower_to_field = {field.lower(): field for field in fields}

    for candidate in exact_candidates or []:
        field = lower_to_field.get(candidate.lower())
        if field:
            return field

    for keywords in keyword_groups or []:
        for field in fields:
            field_lower = field.lower()
            if all(keyword.lower() in field_lower for keyword in keywords):
                return field

    return None


def numeric_series_for_field(rows, field):
    if not field:
        return []
    return [parse_number(row.get(field)) for row in rows]


def percent_series_for_field(rows, field):
    if not field:
        return []
    return [parse_percent(row.get(field)) for row in rows]


def find_first_available_field(rows, exact_candidates=None, keyword_groups=None):
    fields = available_csv_fields(rows)
    lower_to_field = {field.lower(): field for field in fields}

    for candidate in exact_candidates or []:
        if not candidate:
            continue
        field = lower_to_field.get(candidate.lower())
        if field:
            return field

    for keywords in keyword_groups or []:
        for field in fields:
            field_lower = field.lower()
            if all(keyword.lower() in field_lower for keyword in keywords):
                return field

    return None


def choose_chat_percent_fields(rows):
    """Return chat percentage lines in the order used by the Looker examples."""
    ordered_percent_candidates = [
        os.getenv("CHAT_AI_CSAT_FIELD", ""),
        "AI CSAT Chat [%]",
        os.getenv("CHAT_AI_CSAT_PARTICIPATION_FIELD", ""),
        "AI CSAT Chat Participation Rate [%]",
        os.getenv("CHAT_CSAT_FIELD", ""),
        "CSAT Chat [%]",
        "Chat CSAT [%]",
        os.getenv("CHAT_CSAT_PARTICIPATION_FIELD", ""),
        "CSAT Chat Participation [%]",
        "CSAT Chat Participation Rate [%]",
        "Chat CSAT Participation [%]",
        "Chat CSAT Participation Rate [%]",
    ]

    fields = available_csv_fields(rows)
    lower_to_field = {field.lower(): field for field in fields}
    percent_fields = []
    seen = set()

    for candidate in ordered_percent_candidates:
        if not candidate:
            continue
        field = lower_to_field.get(candidate.lower())
        if field and field not in seen:
            percent_fields.append(field)
            seen.add(field)

    # Fallback for unusual chat exports: add any remaining chat CSAT /
    # participation percentage fields in source order.
    for field in fields:
        field_lower = field.lower()
        if field in seen:
            continue
        if "%" not in field:
            continue
        if "chat" in field_lower and (
            "csat" in field_lower
            or "participation" in field_lower
        ):
            percent_fields.append(field)
            seen.add(field)

    return percent_fields


def choose_agent_chat_fields(rows):
    count_field = find_first_available_field(
        rows,
        exact_candidates=[
            os.getenv("AGENT_CHAT_COUNT_FIELD", ""),
            os.getenv("CHAT_COUNT_FIELD", ""),
            "Agent Handled Chats [#]",
            "Eligible Chats [#]",
            "Total Chats [#]",
            "Chats [#]",
            "Conversations [#]",
            "Solved Chats [#]",
        ],
        keyword_groups=[
            ["agent", "handled", "chat", "#"],
            ["eligible", "chat", "#"],
            ["total", "chat", "#"],
            ["chat", "#"],
            ["conversation", "#"],
        ],
    )

    percent_fields = choose_chat_percent_fields(rows)

    if not count_field and not percent_fields:
        raise RuntimeError(
            "Could not identify agent chat metrics in the chats CSV. Available fields were: "
            + ", ".join(available_csv_fields(rows))
            + ". Set AGENT_CHAT_COUNT_FIELD / CHAT_* env vars in the workflow if needed."
        )

    return count_field, percent_fields


def choose_bot_chat_fields(rows):
    count_field = find_first_available_field(
        rows,
        exact_candidates=[
            os.getenv("BOT_CHAT_COUNT_FIELD", ""),
            "Bot Handled Chats [#]",
            "Bot Chats [#]",
            "Bot Conversations [#]",
            "Automation Handled Chats [#]",
        ],
        keyword_groups=[
            ["bot", "handled", "chat", "#"],
            ["bot", "chat", "#"],
            ["bot", "conversation", "#"],
            ["automation", "chat", "#"],
        ],
    )

    percent_fields = choose_chat_percent_fields(rows)

    if not count_field:
        raise RuntimeError(
            "Could not identify bot chat count field in the chats CSV. Available fields were: "
            + ", ".join(available_csv_fields(rows))
            + ". Set BOT_CHAT_COUNT_FIELD in the workflow if needed."
        )

    return count_field, percent_fields


def choose_chat_fields(rows):
    """Backward-compatible default: agent chat chart."""
    return choose_agent_chat_fields(rows)


def render_generic_support_chart(rows, title, output_path, count_field=None, percent_fields=None):
    dates = [row[DATE_FIELD] for row in rows]
    percent_fields = percent_fields or []

    count_values = []
    if count_field:
        count_values = [value or 0 for value in numeric_series_for_field(rows, count_field)]

    percent_values_by_field = {
        field: percent_series_for_field(rows, field)
        for field in percent_fields
    }

    scale = 2
    width, height = 1750 * scale, 980 * scale
    plot_left = 215 * scale
    plot_right = 1505 * scale
    plot_top = 140 * scale
    plot_bottom = 670 * scale
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)

    font_title = load_font(38 * scale, bold=True)
    font_axis = load_font(24 * scale, bold=True)
    font_tick = load_font(22 * scale)
    font_label = load_font(21 * scale, bold=True)
    font_legend = load_font(21 * scale)

    colors = {
        "text": (44, 52, 62, 255),
        "grid": (224, 226, 228, 255),
        "axis": (164, 170, 179, 255),
        "label_bg": (255, 255, 255, 225),
        "count": (94, 196, 222, 255),
        "line_1": (111, 154, 148, 255),
        "line_2": (255, 137, 110, 255),
        "line_3": (255, 194, 110, 255),
        "line_4": (130, 116, 198, 255),
    }

    draw_centered_text(draw, (width / 2, 58 * scale), title, font_title, colors["text"])

    max_count = max(count_values) if count_values else 1
    left_axis_max = nice_axis_max(max_count * 1.16)

    all_percent_values = [
        value
        for values in percent_values_by_field.values()
        for value in values
        if value is not None
    ]
    max_percent = max(all_percent_values) if all_percent_values else 1.0
    right_axis_max = 1.0 if max_percent <= 1.0 else min(1.15, math.ceil(max_percent * 10) / 10)

    side_padding = 78 * scale

    def x_at(index):
        if len(dates) == 1:
            return (plot_left + plot_right) / 2
        usable_width = plot_width - (2 * side_padding)
        return plot_left + side_padding + (usable_width * index / (len(dates) - 1))

    def y_left(value):
        return plot_bottom - (value / left_axis_max) * plot_height

    def y_right(value):
        return plot_bottom - (value / right_axis_max) * plot_height

    def draw_value_label(text, center_x, center_y, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        label_x = center_x - text_width / 2
        label_y = center_y - text_height / 2

        label_x = max(plot_left + 4 * scale, min(plot_right - text_width - 4 * scale, label_x))
        label_y = max(plot_top + 4 * scale, min(plot_bottom - text_height - 4 * scale, label_y))

        draw.rounded_rectangle(
            (
                label_x - 4 * scale,
                label_y - 3 * scale,
                label_x + text_width + 4 * scale,
                label_y + text_height + 5 * scale,
            ),
            radius=4 * scale,
            fill=colors["label_bg"],
        )
        draw.text((label_x, label_y), text, font=font, fill=fill)

    for index in range(6):
        tick = left_axis_max * index / 5
        y = y_left(tick)
        draw.line((plot_left, y, plot_right, y), fill=colors["grid"], width=2 * scale)
        label = format_count(tick)
        bbox = draw.textbbox((0, 0), label, font=font_tick)
        draw.text(
            (plot_left - 24 * scale - (bbox[2] - bbox[0]), y - 13 * scale),
            label,
            font=font_tick,
            fill=colors["text"],
        )

    for index in range(6):
        pct = right_axis_max * index / 5
        y = y_right(pct)
        label = f"{pct * 100:.0f}%"
        draw.text((plot_right + 24 * scale, y - 13 * scale), label, font=font_tick, fill=colors["text"])

    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=colors["axis"], width=2 * scale)
    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=colors["axis"], width=2 * scale)
    draw.line((plot_right, plot_top, plot_right, plot_bottom), fill=colors["axis"], width=2 * scale)

    if count_field:
        slot = (plot_width - 2 * side_padding) / max(len(dates) - 1, 1)
        bar_width = min(92 * scale, slot * 0.58)
        for index, value in enumerate(count_values):
            x = x_at(index)
            y = y_left(value)
            draw.rounded_rectangle(
                (x - bar_width / 2, y, x + bar_width / 2, plot_bottom),
                radius=4 * scale,
                fill=colors["count"],
            )
            draw_value_label(format_count(value), x, y - 18 * scale, font_label, colors["count"])

    line_colors = [colors["line_1"], colors["line_2"], colors["line_3"], colors["line_4"]]

    for field_index, (field, values) in enumerate(percent_values_by_field.items()):
        color = line_colors[field_index % len(line_colors)]
        points = []
        for index, value in enumerate(values):
            if value is None:
                continue
            points.append((x_at(index), y_right(value), value, index))

        if len(points) >= 2:
            draw.line([(x, y) for x, y, _, _ in points], fill=color, width=4 * scale)

        for x, y, value, index in points:
            draw.ellipse((x - 7 * scale, y - 7 * scale, x + 7 * scale, y + 7 * scale), fill=color)
            draw_value_label(
                format_percent(value),
                x,
                y - (26 + field_index * 22) * scale,
                font_label,
                color,
            )

    for index, date_label in enumerate(dates):
        x = x_at(index)
        display_date = date_label[5:] if len(date_label) >= 10 else date_label
        bbox = draw.textbbox((0, 0), display_date, font=font_tick)
        draw.text(
            (x - (bbox[2] - bbox[0]) / 2, plot_bottom + 22 * scale),
            display_date,
            font=font_tick,
            fill=colors["text"],
        )

    draw_centered_text(
        draw,
        ((plot_left + plot_right) / 2, plot_bottom + 72 * scale),
        DATE_FIELD,
        font_axis,
        colors["text"],
    )

    left_axis_label = count_field or "Count"
    draw_rotated_text(
        image,
        (58 * scale, (plot_top + plot_bottom) / 2),
        left_axis_label,
        font_axis,
        colors["count"],
        90,
    )
    draw_rotated_text(
        image,
        (1680 * scale, (plot_top + plot_bottom) / 2),
        "[%]",
        font_axis,
        colors["text"],
        90,
    )

    legend_items = []
    if count_field:
        legend_items.append((count_field, colors["count"]))
    for field_index, field in enumerate(percent_values_by_field.keys()):
        legend_items.append((field, line_colors[field_index % len(line_colors)]))

    legend_y = 850 * scale
    legend_total_width = 0
    for text, _ in legend_items:
        bbox = draw.textbbox((0, 0), text, font=font_legend)
        legend_total_width += 38 * scale + (bbox[2] - bbox[0]) + 32 * scale

    legend_x = max(60 * scale, (width - legend_total_width) / 2)
    for text, color in legend_items:
        draw.ellipse((legend_x, legend_y - 8 * scale, legend_x + 16 * scale, legend_y + 8 * scale), fill=color)
        draw.text((legend_x + 24 * scale, legend_y - 13 * scale), text, font=font_legend, fill=colors["text"])
        bbox = draw.textbbox((0, 0), text, font=font_legend)
        legend_x += 38 * scale + (bbox[2] - bbox[0]) + 32 * scale

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = image.convert("RGB").resize((width // scale, height // scale), Image.Resampling.LANCZOS)
    image.save(output_path, "PNG", optimize=True)
    return output_path


def render_chat_chart(rows, output_path, chart_kind="agent"):
    if chart_kind == "agent":
        count_field, percent_fields = choose_agent_chat_fields(rows)
        title = "Agent Chat Metrics - CSAT"
        print(f"Agent chat count field: {count_field}")
        print(f"Agent chat percent fields: {percent_fields}")
    elif chart_kind == "bot":
        count_field, percent_fields = choose_bot_chat_fields(rows)
        title = "Bot Chat Metrics - CSAT"
        print(f"Bot chat count field: {count_field}")
        print(f"Bot chat percent fields: {percent_fields}")
    else:
        raise RuntimeError(f"Unknown chat chart kind: {chart_kind}")

    return render_generic_support_chart(
        rows=rows,
        title=title,
        output_path=output_path,
        count_field=count_field,
        percent_fields=percent_fields,
    )

def generate_and_insert_first_line_charts(page_id):
    print()
    print("=" * 100)
    print("STEP 2: GENERATE AND INSERT FIRST LINE SUPPORT CHARTS")
    print("=" * 100)
    print(f"Target WBR page: {page_id}")

    notion_request("GET", f"/pages/{page_id}")
    print("Target page read successful.")

    cleanup_existing_first_line_auto_charts(page_id)

    inbound_weekly_csv = find_csv_by_report_slug(
        "first-line-inbound-weekly",
        explicit_path=CSV_INBOUND_WEEKLY_PATH,
    )
    inbound_8_week_csv = find_csv_by_report_slug(
        "first-line-inbound-8-week",
        explicit_path=CSV_INBOUND_8_WEEK_PATH,
    )
    outbound_8_week_csv = find_csv_by_report_slug(
        "first-line-outbound-8-week",
        explicit_path=CSV_OUTBOUND_8_WEEK_PATH,
    )
    chats_csv = find_csv_by_report_slug(
        "first-line-chats",
        explicit_path=CSV_CHATS_PATH,
    )

    print(f"Inbound weekly CSV: {inbound_weekly_csv}")
    print(f"Inbound 8-week CSV: {inbound_8_week_csv}")
    print(f"Outbound 8-week CSV: {outbound_8_week_csv}")
    print(f"Chats CSV: {chats_csv}")

    inbound_weekly_rows = read_metrics_csv(inbound_weekly_csv)
    inbound_8_week_rows = read_metrics_csv(inbound_8_week_csv)
    outbound_8_week_rows = read_metrics_csv(outbound_8_week_csv)
    chats_rows = read_metrics_csv(chats_csv)

    review_week = latest_date_from_rows(inbound_weekly_rows)

    chart_specs = [
        {
            "key": "inbound_weekly",
            "title": "1 Week View",
            "anchor_text": TARGET_INBOUND_WEEKLY_ANCHOR_TEXT,
            "rows": inbound_weekly_rows,
            "renderer": "call",
            "include_abandoned": True,
            "fill_missing_percent_as_zero": True,
            "output_name": "first_line_1_week_view.png",
            "caption": "First Line Support inbound call metrics — 1-week daily view.",
        },
        {
            "key": "inbound_8_week",
            "title": "Inbound",
            "anchor_text": TARGET_INBOUND_8_WEEK_ANCHOR_TEXT,
            "rows": inbound_8_week_rows,
            "renderer": "call",
            "include_abandoned": True,
            "fill_missing_percent_as_zero": False,
            "output_name": "first_line_inbound.png",
            "caption": "First Line Support inbound call metrics — 8-week rolling window.",
        },
        {
            "key": "outbound_8_week",
            "title": "Outbound",
            "anchor_text": TARGET_OUTBOUND_8_WEEK_ANCHOR_TEXT,
            "rows": outbound_8_week_rows,
            "renderer": "call",
            "include_abandoned": False,
            "fill_missing_percent_as_zero": False,
            "output_name": "first_line_outbound.png",
            "caption": "First Line Support outbound call metrics — 8-week rolling window.",
        },
        {
            "key": "agent_chats",
            "title": "Agent Chat Metrics - CSAT",
            "anchor_text": TARGET_CHATS_ANCHOR_TEXT,
            "rows": chats_rows,
            "renderer": "chat_agent",
            "output_name": "first_line_agent_chats.png",
            "caption": "First Line Support agent chat metrics.",
        },
        {
            "key": "bot_chats",
            "title": "Bot Chat Metrics - CSAT",
            "anchor_text": TARGET_CHATS_ANCHOR_TEXT,
            "rows": chats_rows,
            "renderer": "chat_bot",
            "output_name": "first_line_bot_chats.png",
            "caption": "First Line Support bot chat metrics.",
        },
    ]

    uploaded_charts = []

    for chart in chart_specs:
        output_path = OUTPUT_DIR / review_week / chart["output_name"]

        if chart["renderer"] == "chat_agent":
            render_chat_chart(
                rows=chart["rows"],
                output_path=output_path,
                chart_kind="agent",
            )
        elif chart["renderer"] == "chat_bot":
            render_chat_chart(
                rows=chart["rows"],
                output_path=output_path,
                chart_kind="bot",
            )
        else:
            render_combined_call_chart(
                rows=chart["rows"],
                title=chart["title"],
                output_path=output_path,
                include_abandoned=chart["include_abandoned"],
                fill_missing_percent_as_zero=chart.get("fill_missing_percent_as_zero", False),
            )

        print(f"Rendered chart: {output_path}")

        upload = upload_file_to_notion(output_path)
        uploaded_charts.append(
            {
                "key": chart["key"],
                "title": chart["title"],
                "caption": chart["caption"],
                "file_upload_id": upload["id"],
            }
        )
        print(f"Uploaded chart to Notion: {chart['title']}")

    append_first_line_chart_set(
        page_id=page_id,
        chart_specs=chart_specs,
        uploaded_charts=uploaded_charts,
        review_week=review_week,
    )

    print("Chart insertion success.")
    return review_week


# =============================================================================
# MAIN
# =============================================================================

def print_setup():
    print("=" * 100)
    print("LOCAL WBR ALL-IN-ONE AUTOMATION")
    print("=" * 100)
    print(f"Build script version: {BUILD_WBR_VERSION}")
    print(f"Current folder: {Path.cwd()}")
    print(f"Mode: {WBR_AUTOMATION_MODE}")
    print(f"NOTION_DATABASE_ID: {NOTION_DATABASE_ID}")
    print(f"TEMPLATE_ITEM_TITLE: {TEMPLATE_ITEM_TITLE}")
    print(f"REPORTING_WEEK: {REPORTING_WEEK}")
    print(f"NEW_WBR_TITLE: {NEW_WBR_TITLE}")
    print(f"CSV_DIR: {CSV_DIR}")
    print(f"TARGET_HEADING: {TARGET_HEADING}")
    print(f"TARGET_CHATS_HEADING: {TARGET_CHATS_HEADING}")
    print()


def main():
    print_setup()

    if WBR_AUTOMATION_MODE not in {"full", "copy_only", "charts_only"}:
        raise RuntimeError(
            "Invalid WBR_AUTOMATION_MODE. Use one of: full, copy_only, charts_only."
        )

    if not NOTION_TOKEN:
        raise RuntimeError(
            "Missing NOTION_TOKEN. Run:\n"
            'export NOTION_TOKEN="your_notion_token"'
        )

    if WBR_AUTOMATION_MODE == "charts_only":
        target_page_id = os.getenv("NOTION_PAGE_ID") or os.getenv("NEW_NOTION_PAGE_ID")
        if not target_page_id:
            raise RuntimeError(
                "WBR_AUTOMATION_MODE=charts_only requires NOTION_PAGE_ID or NEW_NOTION_PAGE_ID."
            )

        page_id = normalize_page_id(target_page_id)
        generate_and_insert_first_line_charts(page_id)
        print("ALL DONE")
        return

    new_page_id, new_page_url = create_wbr_item()

    if WBR_AUTOMATION_MODE == "copy_only":
        print("WBR_AUTOMATION_MODE=copy_only, stopping before chart insertion.")
        print(f"NEW_NOTION_PAGE_ID={new_page_id}")
        print(f"NEW_NOTION_PAGE_URL={new_page_url}")
        return

    generate_and_insert_first_line_charts(normalize_page_id(new_page_id))

    print()
    print("=" * 100)
    print("ALL DONE")
    print("=" * 100)
    print(f"NEW_NOTION_PAGE_ID={new_page_id}")
    print(f"NEW_NOTION_PAGE_URL={new_page_url}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
