import csv
import hashlib
import os
from datetime import date, datetime
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

# --- YOUR CONFIGURATION ---
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
DATA_SOURCE_ID = os.getenv("NOTION_DATA_SOURCE_ID")

# GitHub Actions will usually pass CSV_PATH explicitly.
# If CSV_PATH is not set, the script will pick the newest CSV from CSV_GLOB.
CSV_PATH = Path(os.environ["CSV_PATH"]) if os.getenv("CSV_PATH") else None
CSV_GLOB = os.getenv("CSV_GLOB", "data/incoming/*.csv")

VIEWS_API_VERSION = "2026-03-11"
CHART_VIEW_PREFIX = "WoW - "
WBR_VIEW_PREFIX = "WBR Test - "
WBR_DASHBOARD_NAME = f"{WBR_VIEW_PREFIX}Overview"
WBR_REPORT_TITLE_PREFIX = "WBR Combo Chart Test - "
WBR_OUTPUT_DIR = Path("wbr_outputs")

# Notion percent fields store values as fractions when the property is formatted
# as percent, so 66.51% is sent as 0.6651 and displays as 66.51%.
PERCENT_FIELDS = (
    "AI CSAT Call [%]",
    "AI CSAT Call Participation Rate [%]",
    "Abandoned Call Rate [%]",
    "Calls Within Hunting Time SLA [%]",
)

NUMBER_FIELDS = (
    "Eligible Calls [#]",
    "Average Call Hunting Time [min]",
    "Average Call Handling Time [min]",
)

DATE_FIELD = "Interaction Date Dynamic"

# WBR snapshot metadata.
# Each CSV represents one rolling WBR review window.
# The latest date in the CSV becomes the WBR Review Week.
WBR_REVIEW_WEEK_FIELD = "WBR Review Week"
WBR_REVIEW_KEY_FIELD = "WBR Review Key"
UNIQUE_ROW_KEY_FIELD = "Unique Row Key"
SOURCE_FILE_FIELD = "Source File"
FILE_HASH_FIELD = "File Hash"
WINDOW_START_FIELD = "Window Start"
WINDOW_END_FIELD = "Window End"

METRIC_FIELDS = (
    "Eligible Calls [#]",
    "AI CSAT Call [%]",
    "AI CSAT Call Participation Rate [%]",
    "Abandoned Call Rate [%]",
    "Calls Within Hunting Time SLA [%]",
    "Average Call Hunting Time [min]",
    "Average Call Handling Time [min]",
)
CHART_COLOR_THEMES = (
    "blue",
    "green",
    "teal",
    "orange",
    "purple",
    "pink",
    "red",
)
CHART_AGGREGATORS = {
    "Eligible Calls [#]": "sum",
    "AI CSAT Call [%]": "average",
    "AI CSAT Call Participation Rate [%]": "average",
    "Abandoned Call Rate [%]": "average",
    "Calls Within Hunting Time SLA [%]": "average",
    "Average Call Hunting Time [min]": "average",
    "Average Call Handling Time [min]": "average",
}
WBR_TARGETS = {
    "AI CSAT Call [%]": {
        "value": 0.70,
        "label": "Example target 70%",
        "color": "green",
        "dash_style": "dash",
    },
    "AI CSAT Call Participation Rate [%]": {
        "value": 0.75,
        "label": "Example target 75%",
        "color": "green",
        "dash_style": "dash",
    },
    "Abandoned Call Rate [%]": {
        "value": 0.03,
        "label": "Example max 3%",
        "color": "red",
        "dash_style": "dash",
    },
    "Calls Within Hunting Time SLA [%]": {
        "value": 0.85,
        "label": "Example target 85%",
        "color": "green",
        "dash_style": "dash",
    },
    "Average Call Hunting Time [min]": {
        "value": 1.00,
        "label": "Example max 1.0 min",
        "color": "orange",
        "dash_style": "dash",
    },
    "Average Call Handling Time [min]": {
        "value": 5.00,
        "label": "Example benchmark 5.0 min",
        "color": "orange",
        "dash_style": "dash",
    },
}
WBR_KPI_FIELDS = (
    "Eligible Calls [#]",
    "AI CSAT Call [%]",
    "Abandoned Call Rate [%]",
    "Calls Within Hunting Time SLA [%]",
)
WBR_TREND_FIELDS = (
    "Eligible Calls [#]",
    "AI CSAT Call [%]",
    "AI CSAT Call Participation Rate [%]",
    "Abandoned Call Rate [%]",
    "Calls Within Hunting Time SLA [%]",
    "Average Call Hunting Time [min]",
    "Average Call Handling Time [min]",
)

PROPERTY_SCHEMAS = {
    DATE_FIELD: {"date": {}},
    WBR_REVIEW_WEEK_FIELD: {"date": {}},
    WBR_REVIEW_KEY_FIELD: {"rich_text": {}},
    UNIQUE_ROW_KEY_FIELD: {"rich_text": {}},
    SOURCE_FILE_FIELD: {"rich_text": {}},
    FILE_HASH_FIELD: {"rich_text": {}},
    WINDOW_START_FIELD: {"date": {}},
    WINDOW_END_FIELD: {"date": {}},
    "Eligible Calls [#]": {"number": {"format": "number_with_commas"}},
    "AI CSAT Call [%]": {"number": {"format": "percent"}},
    "AI CSAT Call Participation Rate [%]": {"number": {"format": "percent"}},
    "Abandoned Call Rate [%]": {"number": {"format": "percent"}},
    "Calls Within Hunting Time SLA [%]": {"number": {"format": "percent"}},
    "Average Call Hunting Time [min]": {"number": {"format": "number"}},
    "Average Call Handling Time [min]": {"number": {"format": "number"}},
}

BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
VIEW_HEADERS = {
    **HEADERS,
    "Notion-Version": VIEWS_API_VERSION,
}


def notion_request(method, path, headers=None, **kwargs):
    response = requests.request(
        method,
        f"{BASE_URL}{path}",
        headers=headers or HEADERS,
        **kwargs,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Notion API request failed: {method} {path}\n"
            f"Status Code: {response.status_code}\n"
            f"Response: {response.text}"
        )

    return response.json()


def get_title_property_name(database):
    for property_name, property_config in database["properties"].items():
        if property_config["type"] == "title":
            return property_name

    raise RuntimeError("Could not find a title property in the Notion database.")


def ensure_database_properties(database):
    existing_properties = database["properties"]
    incompatible_properties = []
    properties_to_update = {}
    properties_to_add = {
        name: schema
        for name, schema in PROPERTY_SCHEMAS.items()
        if name not in existing_properties
    }

    for property_name, property_schema in PROPERTY_SCHEMAS.items():
        if property_name not in existing_properties:
            continue

        expected_type = next(iter(property_schema))
        actual_type = existing_properties[property_name]["type"]
        if actual_type != expected_type:
            incompatible_properties.append((property_name, expected_type, actual_type))
            continue

        if expected_type == "number":
            expected_format = property_schema["number"]["format"]
            actual_format = existing_properties[property_name]["number"]["format"]
            if actual_format != expected_format:
                properties_to_update[property_name] = property_schema

    if incompatible_properties:
        details = "\n".join(
            f"  - {name}: expected {expected_type}, found {actual_type}"
            for name, expected_type, actual_type in incompatible_properties
        )
        raise RuntimeError(
            "Some Notion database properties exist with incompatible types:\n"
            f"{details}\n"
            "Please rename or fix these properties in Notion, then rerun the script."
        )

    properties_to_patch = {**properties_to_add, **properties_to_update}

    if not properties_to_patch:
        print("All required Notion database properties already exist with the right types.")
        return

    if properties_to_add:
        print("Adding missing Notion database properties:")
        for property_name in properties_to_add:
            print(f"  - {property_name}")

    if properties_to_update:
        print("Updating Notion number formats:")
        for property_name in properties_to_update:
            print(f"  - {property_name}")

    notion_request(
        "PATCH",
        f"/databases/{DATABASE_ID}",
        json={"properties": properties_to_patch},
    )


def clean_value(value):
    value = value.strip()
    return value if value else None


def parse_number(value):
    value = clean_value(value)
    if value is None:
        return None

    return float(value.replace(",", ""))


def parse_percent(value):
    value = clean_value(value)
    if value is None:
        return None

    return float(value.replace("%", "").replace(",", "")) / 100


def parse_metric_value(row, metric_name):
    if metric_name in PERCENT_FIELDS:
        return parse_percent(row[metric_name])

    return parse_number(row[metric_name])


def sorted_rows_by_date(csv_rows):
    return sorted(
        csv_rows,
        key=lambda row: date.fromisoformat(row[DATE_FIELD]),
    )


def format_metric_value(metric_name, value):
    if value is None:
        return "n/a"

    if metric_name in PERCENT_FIELDS:
        return f"{value * 100:.2f}%"

    if metric_name == "Eligible Calls [#]":
        return f"{value:,.0f}"

    return f"{value:.2f}"


def format_wow_change(metric_name, current_value, previous_value):
    if current_value is None or previous_value is None:
        return "WoW n/a"

    difference = current_value - previous_value

    if metric_name in PERCENT_FIELDS:
        return f"WoW {difference * 100:+.2f} pp"

    if previous_value == 0:
        return f"WoW {difference:+.2f}"

    percent_change = difference / previous_value
    return f"WoW {difference:+.2f} ({percent_change:+.1%})"


def build_wbr_metric_summary(csv_rows):
    rows = sorted_rows_by_date(csv_rows)
    if not rows:
        raise ValueError("Cannot build WBR views without CSV rows.")

    latest_row = rows[-1]
    previous_row = rows[-2] if len(rows) > 1 else None
    summary = {
        "latest_date": latest_row[DATE_FIELD],
        "previous_date": previous_row[DATE_FIELD] if previous_row else None,
        "metrics": {},
    }

    for metric_name in METRIC_FIELDS:
        current_value = parse_metric_value(latest_row, metric_name)
        previous_value = parse_metric_value(previous_row, metric_name) if previous_row else None
        summary["metrics"][metric_name] = {
            "current": current_value,
            "previous": previous_value,
            "caption": (
                f"Current week {summary['latest_date']}: "
                f"{format_metric_value(metric_name, current_value)}; "
                f"{format_wow_change(metric_name, current_value, previous_value)}"
            ),
        }

    return summary


def load_font(size, bold=False):
    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Helvetica.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    ]

    for font_path in font_candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size=size)

    return ImageFont.load_default()


def draw_centered_text(draw, xy, text, font, fill):
    x, y = xy
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (right - left) / 2, y - (bottom - top) / 2), text, font=font, fill=fill)


def draw_rotated_text(image, xy, text, font, fill, angle):
    text_bbox = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    text_image = Image.new("RGBA", (text_width + 12, text_height + 12), (255, 255, 255, 0))
    text_draw = ImageDraw.Draw(text_image)
    text_draw.text((6, 6), text, font=font, fill=fill)
    rotated = text_image.rotate(angle, expand=True)
    image.alpha_composite(rotated, (int(xy[0] - rotated.width / 2), int(xy[1] - rotated.height / 2)))


def nice_axis_max(value):
    if value <= 10:
        return 10

    if value <= 25:
        return 25

    if value <= 50:
        return 50

    if value <= 100:
        return 100

    if value <= 500:
        return ((int(value) + 99) // 100) * 100

    return ((int(value) + 499) // 500) * 500


def axis_ticks(axis_max, steps=5):
    return [axis_max * index / steps for index in range(steps + 1)]


def render_wbr_combo_chart(csv_rows, output_path):
    rows = sorted_rows_by_date(csv_rows)
    dates = [row[DATE_FIELD] for row in rows]
    eligible_calls = [parse_number(row["Eligible Calls [#]"]) or 0 for row in rows]
    csat = [parse_percent(row["AI CSAT Call [%]"]) or 0 for row in rows]
    participation = [parse_percent(row["AI CSAT Call Participation Rate [%]"]) or 0 for row in rows]
    abandoned = [parse_percent(row["Abandoned Call Rate [%]"]) or 0 for row in rows]

    scale = 2
    width, height = 1600 * scale, 1120 * scale
    card_margin = 34 * scale
    plot_left = 210 * scale
    plot_right = 1305 * scale
    plot_top = 165 * scale
    plot_bottom = 805 * scale
    image = Image.new("RGBA", (width, height), (244, 246, 248, 255))
    draw = ImageDraw.Draw(image)

    font_title = load_font(42 * scale, bold=True)
    font_axis = load_font(27 * scale)
    font_axis_bold = load_font(30 * scale, bold=True)
    font_tick = load_font(26 * scale)
    font_label = load_font(28 * scale, bold=True)
    font_legend = load_font(28 * scale)

    colors = {
        "card": (255, 255, 255, 255),
        "grid": (224, 226, 228, 255),
        "text": (39, 46, 53, 255),
        "muted": (111, 119, 126, 255),
        "eligible": (94, 196, 222, 255),
        "csat": (255, 137, 110, 255),
        "participation": (255, 194, 110, 255),
        "abandoned": (111, 154, 148, 255),
    }

    draw.rounded_rectangle(
        (card_margin, card_margin, width - card_margin, height - card_margin),
        radius=18 * scale,
        fill=colors["card"],
        outline=(228, 230, 233, 255),
        width=2 * scale,
    )

    draw_centered_text(
        draw,
        (width / 2, 78 * scale),
        "Call Metrics - Inbound",
        font_title,
        colors["text"],
    )

    left_axis_max = nice_axis_max(max(eligible_calls) * 1.1)
    right_axis_max = 1.0
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    bar_slot = plot_width / max(len(dates), 1)

    def x_at(index):
        return plot_left + bar_slot * (index + 0.5)

    def y_left(value):
        return plot_bottom - (value / left_axis_max) * plot_height

    def y_right(value):
        return plot_bottom - (value / right_axis_max) * plot_height

    for tick in axis_ticks(left_axis_max):
        y = y_left(tick)
        draw.line((plot_left, y, plot_right, y), fill=colors["grid"], width=2 * scale)
        tick_label = f"{tick:,.0f}"
        tick_left, _, tick_right, _ = draw.textbbox((0, 0), tick_label, font=font_tick)
        draw.text(
            (plot_left - 24 * scale - (tick_right - tick_left), y - 15 * scale),
            tick_label,
            font=font_tick,
            fill=colors["text"],
        )

    for pct in range(0, 101, 20):
        y = y_right(pct / 100)
        draw.text(
            (plot_right + 34 * scale, y - 15 * scale),
            f"{pct:.2f}%",
            font=font_tick,
            fill=colors["text"],
        )

    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=(190, 196, 202, 255), width=2 * scale)

    bar_width = min(112 * scale, bar_slot * 0.72)

    for index, value in enumerate(eligible_calls):
        x = x_at(index)
        y = y_left(value)
        draw.rectangle(
            (x - bar_width / 2, y, x + bar_width / 2, plot_bottom),
            fill=colors["eligible"],
        )
        draw_centered_text(
            draw,
            (x, y - 26 * scale),
            f"{value:.0f}",
            font_label,
            colors["eligible"],
        )

    def draw_series(values, color):
        points = [(x_at(index), y_right(value)) for index, value in enumerate(values)]
        if len(points) > 1:
            draw.line(points, fill=color, width=5 * scale, joint="curve")

        for index, (x, y) in enumerate(points):
            draw.ellipse(
                (x - 10 * scale, y - 10 * scale, x + 10 * scale, y + 10 * scale),
                fill=color,
            )
            label_y = y - 30 * scale
            draw_centered_text(
                draw,
                (x, label_y),
                f"{values[index] * 100:.2f}%",
                font_label,
                color,
            )

    draw_series(csat, colors["csat"])
    draw_series(participation, colors["participation"])
    draw_series(abandoned, colors["abandoned"])

    for index, label in enumerate(dates):
        x = x_at(index)
        draw_rotated_text(image, (x, plot_bottom + 75 * scale), label, font_tick, colors["text"], 45)

    draw_centered_text(
        draw,
        ((plot_left + plot_right) / 2, plot_bottom + 155 * scale),
        DATE_FIELD,
        font_axis,
        colors["text"],
    )
    draw_rotated_text(
        image,
        (75 * scale, (plot_top + plot_bottom) / 2),
        "Eligible Calls [#]",
        font_axis_bold,
        colors["eligible"],
        90,
    )
    draw_rotated_text(
        image,
        (1510 * scale, (plot_top + plot_bottom) / 2),
        "[%]",
        font_axis_bold,
        colors["text"],
        90,
    )

    legend_items = [
        ("Eligible Calls [#]", colors["eligible"], "bar"),
        ("AI CSAT Call [%]", colors["csat"], "line"),
        ("Abandoned Call Rate [%]", colors["abandoned"], "line"),
        ("AI CSAT Call Participation Rate [%]", colors["participation"], "line"),
    ]
    legend_y = 1002 * scale
    legend_x_positions = [350 * scale, 350 * scale, 880 * scale, 880 * scale]
    legend_y_offsets = [0, 52 * scale, 0, 52 * scale]

    for (text, color, marker_type), x, y_offset in zip(legend_items, legend_x_positions, legend_y_offsets):
        y = legend_y + y_offset
        if marker_type == "bar":
            draw.ellipse((x - 13 * scale, y - 13 * scale, x + 13 * scale, y + 13 * scale), fill=color)
        else:
            draw.line((x - 18 * scale, y, x + 18 * scale, y), fill=color, width=5 * scale)
            draw.ellipse((x - 10 * scale, y - 10 * scale, x + 10 * scale, y + 10 * scale), fill=color)
        draw.text((x + 32 * scale, y - 17 * scale), text, font=font_legend, fill=colors["text"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = image.convert("RGB").resize((width // scale, height // scale), Image.Resampling.LANCZOS)
    image.save(output_path, "PNG", optimize=True)
    return output_path


def build_page_properties(row, title_property_name, wbr_context):
    interaction_date = clean_value(row[DATE_FIELD])
    if not interaction_date:
        raise ValueError(f"Missing required date field: {DATE_FIELD}")

    unique_row_key = f"{wbr_context['review_week']}::{interaction_date}"

    properties = {
        title_property_name: {
            "title": [
                {
                    "text": {
                        "content": f"{wbr_context['review_week']} - {interaction_date}",
                    }
                }
            ],
        },
        DATE_FIELD: {
            "date": {"start": interaction_date},
        },
        WBR_REVIEW_WEEK_FIELD: {
            "date": {"start": wbr_context["review_week"]},
        },
        WBR_REVIEW_KEY_FIELD: {
            "rich_text": [{"text": {"content": wbr_context["review_key"]}}],
        },
        UNIQUE_ROW_KEY_FIELD: {
            "rich_text": [{"text": {"content": unique_row_key}}],
        },
        SOURCE_FILE_FIELD: {
            "rich_text": [{"text": {"content": wbr_context["source_file"]}}],
        },
        FILE_HASH_FIELD: {
            "rich_text": [{"text": {"content": wbr_context["file_hash"]}}],
        },
        WINDOW_START_FIELD: {
            "date": {"start": wbr_context["window_start"]},
        },
        WINDOW_END_FIELD: {
            "date": {"start": wbr_context["window_end"]},
        },
    }

    for field_name in NUMBER_FIELDS:
        number = parse_number(row[field_name])
        if number is not None:
            properties[field_name] = {"number": number}

    for field_name in PERCENT_FIELDS:
        percent = parse_percent(row[field_name])
        if percent is not None:
            properties[field_name] = {"number": percent}

    return properties


def resolve_csv_path():
    """Return the CSV path to import.

    Priority:
    1. CSV_PATH environment variable, passed by GitHub Actions.
    2. Newest CSV matching CSV_GLOB, defaulting to data/incoming/*.csv.
    """
    if CSV_PATH:
        return CSV_PATH

    csv_files = sorted(
        Path(".").glob(CSV_GLOB),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not csv_files:
        raise FileNotFoundError(
            f"No CSV files found. Set CSV_PATH or add a file matching CSV_GLOB={CSV_GLOB!r}."
        )

    return csv_files[0]


def file_sha256(file_path):
    hasher = hashlib.sha256()

    with file_path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            hasher.update(chunk)

    return hasher.hexdigest()


def build_wbr_context(csv_path, csv_rows):
    rows = sorted_rows_by_date(csv_rows)
    if not rows:
        raise ValueError("Cannot build WBR context without CSV rows.")

    window_start = rows[0][DATE_FIELD]
    window_end = rows[-1][DATE_FIELD]
    review_week = window_end

    return {
        "review_week": review_week,
        "review_key": f"WBR::{review_week}",
        "window_start": window_start,
        "window_end": window_end,
        "source_file": csv_path.name,
        "file_hash": file_sha256(csv_path),
    }


def wbr_review_filter(review_week):
    return {
        "property": WBR_REVIEW_WEEK_FIELD,
        "date": {"equals": review_week},
    }


def read_csv_rows(csv_path):
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file was not found: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.reader(csv_file)
        rows = list(reader)

    if len(rows) < 3:
        raise ValueError("CSV must contain the grouping row, header row, and at least one data row.")

    field_names = rows[1]
    data_rows = rows[2:]
    required_fields = {DATE_FIELD, *NUMBER_FIELDS, *PERCENT_FIELDS}
    missing_fields = required_fields.difference(field_names)

    if missing_fields:
        raise ValueError(f"CSV is missing required columns: {sorted(missing_fields)}")

    return [
        dict(zip(field_names, row))
        for row in data_rows
        if any(clean_value(value) for value in row)
    ]


def create_notion_page(properties):
    return notion_request(
        "POST",
        "/pages",
        json={
            "parent": {"database_id": DATABASE_ID},
            "properties": properties,
        },
    )


def find_page_for_date(interaction_date):
    result = notion_request(
        "POST",
        f"/databases/{DATABASE_ID}/query",
        json={
            "filter": {
                "property": DATE_FIELD,
                "date": {"equals": interaction_date},
            },
            "page_size": 1,
        },
    )
    results = result.get("results", [])
    return results[0] if results else None



def find_page_for_unique_row_key(unique_row_key):
    result = notion_request(
        "POST",
        f"/databases/{DATABASE_ID}/query",
        json={
            "filter": {
                "property": UNIQUE_ROW_KEY_FIELD,
                "rich_text": {"equals": unique_row_key},
            },
            "page_size": 1,
        },
    )
    results = result.get("results", [])
    return results[0] if results else None


def update_notion_page(page_id, properties):
    return notion_request(
        "PATCH",
        f"/pages/{page_id}",
        json={"properties": properties},
    )


def find_page_by_title(title_property_name, title):
    result = notion_request(
        "POST",
        f"/databases/{DATABASE_ID}/query",
        json={
            "filter": {
                "property": title_property_name,
                "title": {"equals": title},
            },
            "page_size": 1,
        },
    )
    results = result.get("results", [])
    return results[0] if results else None


def get_or_create_report_page(title_property_name, title):
    existing_page = find_page_by_title(title_property_name, title)
    if existing_page:
        return existing_page

    return create_notion_page(
        {
            title_property_name: {
                "title": [{"text": {"content": title}}],
            },
        }
    )


def upload_file_to_notion(file_path):
    upload = notion_request(
        "POST",
        "/file_uploads",
        headers=VIEW_HEADERS,
        json={},
    )
    upload_headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": VIEWS_API_VERSION,
    }

    with file_path.open("rb") as file_handle:
        response = requests.post(
            upload["upload_url"],
            headers=upload_headers,
            files={"file": (file_path.name, file_handle, "image/png")},
        )

    if response.status_code >= 400:
        raise RuntimeError(
            "Notion file upload failed:\n"
            f"Status Code: {response.status_code}\n"
            f"Response: {response.text}"
        )

    return response.json()


def append_wbr_combo_chart_to_page(page_id, file_upload_id, latest_date):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    children = [
        {
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"Layered Call Metrics - {latest_date}"},
                    }
                ]
            },
        },
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": (
                                "Generated combo chart: eligible calls as bars, "
                                "CSAT, participation, and abandoned rate as percentage lines."
                            )
                        },
                    }
                ]
            },
        },
        {
            "type": "image",
            "image": {
                "type": "file_upload",
                "file_upload": {"id": file_upload_id},
                "caption": [
                    {
                        "type": "text",
                        "text": {"content": f"Generated by script at {timestamp}"},
                    }
                ],
            },
        },
    ]

    notion_request(
        "PATCH",
        f"/blocks/{page_id}/children",
        headers=VIEW_HEADERS,
        json={"children": children},
    )


def push_wbr_combo_chart_to_notion(csv_rows, title_property_name):
    summary = build_wbr_metric_summary(csv_rows)
    latest_date = summary["latest_date"]
    report_title = f"{WBR_REPORT_TITLE_PREFIX}{latest_date}"
    output_path = WBR_OUTPUT_DIR / f"wbr_call_metrics_inbound_{latest_date}.png"

    render_wbr_combo_chart(csv_rows, output_path)
    upload = upload_file_to_notion(output_path)
    report_page = get_or_create_report_page(title_property_name, report_title)
    append_wbr_combo_chart_to_page(report_page["id"], upload["id"], latest_date)

    return {
        "report_title": report_title,
        "page_id": report_page["id"],
        "image_path": str(output_path),
    }


def get_data_source_id(database):
    if DATA_SOURCE_ID:
        return DATA_SOURCE_ID

    data_sources = database.get("data_sources") or []
    if not data_sources:
        raise RuntimeError(
            "Could not find a data source for this database. "
            "Set NOTION_DATA_SOURCE_ID and rerun the script."
        )

    return data_sources[0]["id"]


def retrieve_data_source(data_source_id):
    return notion_request(
        "GET",
        f"/data_sources/{data_source_id}",
        headers=VIEW_HEADERS,
    )


def list_views(params):
    views = []
    request_params = dict(params)

    while True:
        result = notion_request(
            "GET",
            "/views",
            headers=VIEW_HEADERS,
            params=request_params,
        )
        views.extend(result.get("results", []))

        if not result.get("has_more"):
            return views

        request_params["start_cursor"] = result["next_cursor"]


def list_database_views(database_id):
    return list_views({"database_id": database_id})


def list_data_source_views(data_source_id):
    return list_views({"data_source_id": data_source_id})


def retrieve_view(view_id):
    return notion_request(
        "GET",
        f"/views/{view_id}",
        headers=VIEW_HEADERS,
    )


def get_existing_views_by_name(database_id, data_source_id=None):
    views_by_name = {}
    view_references_by_id = {
        view_reference["id"]: view_reference
        for view_reference in list_database_views(database_id)
    }

    if data_source_id:
        view_references_by_id.update(
            {
                view_reference["id"]: view_reference
                for view_reference in list_data_source_views(data_source_id)
            }
        )

    for view_reference in view_references_by_id.values():
        view = retrieve_view(view_reference["id"])
        view_name = view.get("name")
        if view_name and view_name not in views_by_name:
            views_by_name[view_name] = view

    return views_by_name


def build_chart_configuration(date_property_id, metric_property_id, metric_name, color_theme):
    return {
        "type": "chart",
        "chart_type": "line",
        "x_axis": {
            "type": "date",
            "property_id": date_property_id,
            "group_by": "week",
            "start_day_of_week": 1,
            "sort": {"type": "ascending"},
            "hide_empty_groups": True,
        },
        "y_axis": {
            "aggregator": CHART_AGGREGATORS[metric_name],
            "property_id": metric_property_id,
        },
        "x_axis_property_id": None,
        "y_axis_property_id": None,
        "sort": "x_ascending",
        "color_theme": color_theme,
        "height": "large",
        "legend_position": "off",
        "show_data_labels": True,
        "axis_labels": "both",
        "grid_lines": "horizontal",
        "smooth_line": False,
        "hide_line_fill_area": True,
        "caption": "Week-on-week trend grouped by Interaction Date Dynamic.",
    }


def build_chart_payload(data_source_id, properties_by_name, metric_name, color_theme, wbr_context):
    date_property_id = properties_by_name[DATE_FIELD]["id"]
    metric_property_id = properties_by_name[metric_name]["id"]

    return {
        "database_id": DATABASE_ID,
        "data_source_id": data_source_id,
        "name": f"{CHART_VIEW_PREFIX}{metric_name}",
        "type": "chart",
        "filter": wbr_review_filter(wbr_context["review_week"]),
        "sorts": [
            {
                "property": DATE_FIELD,
                "direction": "ascending",
            }
        ],
        "configuration": build_chart_configuration(
            date_property_id,
            metric_property_id,
            metric_name,
            color_theme,
        ),
    }


def build_wbr_number_configuration(metric_property_id, metric_name, color_theme, summary):
    return {
        "type": "chart",
        "chart_type": "number",
        "value": {
            "aggregator": CHART_AGGREGATORS[metric_name],
            "property_id": metric_property_id,
        },
        "color_theme": color_theme,
        "height": "small",
        "hide_title": False,
        "caption": summary["metrics"][metric_name]["caption"],
    }


def build_wbr_trend_configuration(
    date_property_id,
    metric_property_id,
    metric_name,
    color_theme,
    summary,
):
    chart_type = "column" if metric_name == "Eligible Calls [#]" else "line"
    configuration = {
        "type": "chart",
        "chart_type": chart_type,
        "x_axis": {
            "type": "date",
            "property_id": date_property_id,
            "group_by": "week",
            "start_day_of_week": 1,
            "sort": {"type": "ascending"},
            "hide_empty_groups": True,
        },
        "y_axis": {
            "aggregator": CHART_AGGREGATORS[metric_name],
            "property_id": metric_property_id,
        },
        "x_axis_property_id": None,
        "y_axis_property_id": None,
        "sort": "x_ascending",
        "color_theme": color_theme,
        "height": "large",
        "legend_position": "off",
        "show_data_labels": False,
        "axis_labels": "both",
        "grid_lines": "horizontal",
        "caption": summary["metrics"][metric_name]["caption"],
    }

    if chart_type == "line":
        configuration.update(
            {
                "smooth_line": False,
                "hide_line_fill_area": True,
            }
        )

    if metric_name in WBR_TARGETS:
        configuration["reference_lines"] = [WBR_TARGETS[metric_name]]

    return configuration


def build_wbr_kpi_payload(data_source_id, properties_by_name, metric_name, color_theme, summary):
    metric_property_id = properties_by_name[metric_name]["id"]

    return {
        "data_source_id": data_source_id,
        "name": f"{WBR_VIEW_PREFIX}KPI - {metric_name}",
        "type": "chart",
        "filter": {
            "and": [
                wbr_review_filter(summary["latest_date"]),
                {
                    "property": DATE_FIELD,
                    "date": {"equals": summary["latest_date"]},
                },
            ]
        },
        "configuration": build_wbr_number_configuration(
            metric_property_id,
            metric_name,
            color_theme,
            summary,
        ),
    }


def build_wbr_trend_payload(data_source_id, properties_by_name, metric_name, color_theme, summary):
    date_property_id = properties_by_name[DATE_FIELD]["id"]
    metric_property_id = properties_by_name[metric_name]["id"]

    return {
        "data_source_id": data_source_id,
        "name": f"{WBR_VIEW_PREFIX}Trend - {metric_name}",
        "type": "chart",
        "filter": wbr_review_filter(summary["latest_date"]),
        "sorts": [
            {
                "property": DATE_FIELD,
                "direction": "ascending",
            }
        ],
        "configuration": build_wbr_trend_configuration(
            date_property_id,
            metric_property_id,
            metric_name,
            color_theme,
            summary,
        ),
    }


def create_view(payload):
    json_payload = dict(payload)
    if "database_id" in json_payload:
        json_payload["position"] = {"type": "end"}

    return notion_request(
        "POST",
        "/views",
        headers=VIEW_HEADERS,
        json=json_payload,
    )


def update_view(view_id, payload):
    update_payload = {"name": payload["name"]}

    for optional_key in ("filter", "sorts", "configuration"):
        if optional_key in payload:
            update_payload[optional_key] = payload[optional_key]

    return notion_request(
        "PATCH",
        f"/views/{view_id}",
        headers=VIEW_HEADERS,
        json=update_payload,
    )


def sync_chart_views(data_source_id, wbr_context):
    data_source = retrieve_data_source(data_source_id)
    properties_by_name = data_source["properties"]
    missing_properties = [
        property_name
        for property_name in (DATE_FIELD, WBR_REVIEW_WEEK_FIELD, *METRIC_FIELDS)
        if property_name not in properties_by_name
    ]

    if missing_properties:
        raise RuntimeError(
            f"Cannot build chart views because these properties are missing: {missing_properties}"
        )

    existing_views_by_name = get_existing_views_by_name(DATABASE_ID, data_source_id)
    created_count = 0
    updated_count = 0

    for index, metric_name in enumerate(METRIC_FIELDS):
        color_theme = CHART_COLOR_THEMES[index % len(CHART_COLOR_THEMES)]
        payload = build_chart_payload(
            data_source_id,
            properties_by_name,
            metric_name,
            color_theme,
        )
        existing_view = existing_views_by_name.get(payload["name"])

        if existing_view:
            update_view(existing_view["id"], payload)
            updated_count += 1
            print(f"  - Updated chart: {payload['name']}")
        else:
            create_view(payload)
            created_count += 1
            print(f"  - Created chart: {payload['name']}")

    return created_count, updated_count


def ensure_wbr_dashboard(data_source_id, existing_views_by_name):
    existing_dashboard = existing_views_by_name.get(WBR_DASHBOARD_NAME)
    if existing_dashboard:
        return existing_dashboard, False

    dashboard = create_view(
        {
            "database_id": DATABASE_ID,
            "data_source_id": data_source_id,
            "name": WBR_DASHBOARD_NAME,
            "type": "dashboard",
        }
    )
    print(f"  - Created dashboard: {WBR_DASHBOARD_NAME}")
    return dashboard, True


def upsert_wbr_widget(payload, existing_views_by_name):
    existing_view = existing_views_by_name.get(payload["name"])

    if existing_view:
        update_view(existing_view["id"], payload)
        print(f"  - Updated WBR widget: {payload['name']}")
        return "updated"

    create_view(payload)
    print(f"  - Created WBR widget: {payload['name']}")
    return "created"


def sync_wbr_example_views(data_source_id, csv_rows):
    data_source = retrieve_data_source(data_source_id)
    properties_by_name = data_source["properties"]
    missing_properties = [
        property_name
        for property_name in (DATE_FIELD, WBR_REVIEW_WEEK_FIELD, *WBR_TREND_FIELDS)
        if property_name not in properties_by_name
    ]

    if missing_properties:
        raise RuntimeError(
            f"Cannot build WBR views because these properties are missing: {missing_properties}"
        )

    summary = build_wbr_metric_summary(csv_rows)
    existing_views_by_name = get_existing_views_by_name(DATABASE_ID, data_source_id)
    dashboard, dashboard_created = ensure_wbr_dashboard(data_source_id, existing_views_by_name)
    existing_views_by_name[WBR_DASHBOARD_NAME] = dashboard

    created_count = 0
    updated_count = 0

    for index, metric_name in enumerate(WBR_KPI_FIELDS):
        color_theme = CHART_COLOR_THEMES[index % len(CHART_COLOR_THEMES)]
        payload = build_wbr_kpi_payload(
            data_source_id,
            properties_by_name,
            metric_name,
            color_theme,
            summary,
        )
        payload["view_id"] = dashboard["id"]
        payload["placement"] = {"type": "new_row"}
        result = upsert_wbr_widget(payload, existing_views_by_name)
        created_count += result == "created"
        updated_count += result == "updated"

    for index, metric_name in enumerate(WBR_TREND_FIELDS):
        color_theme = CHART_COLOR_THEMES[index % len(CHART_COLOR_THEMES)]
        payload = build_wbr_trend_payload(
            data_source_id,
            properties_by_name,
            metric_name,
            color_theme,
            summary,
        )
        payload["view_id"] = dashboard["id"]
        payload["placement"] = {"type": "new_row"}
        result = upsert_wbr_widget(payload, existing_views_by_name)
        created_count += result == "created"
        updated_count += result == "updated"

    return {
        "dashboard_created": dashboard_created,
        "widgets_created": created_count,
        "widgets_updated": updated_count,
        "latest_date": summary["latest_date"],
        "previous_date": summary["previous_date"],
    }


def main():
    csv_path = resolve_csv_path()
    print(f"Reading CSV: {csv_path}")
    csv_rows = read_csv_rows(csv_path)
    print(f"Found {len(csv_rows)} rows to import.")

    print("Checking Notion database schema...")
    database = notion_request("GET", f"/databases/{DATABASE_ID}")
    title_property_name = get_title_property_name(database)
    ensure_database_properties(database)

    wbr_context = build_wbr_context(csv_path, csv_rows)
    print(
        "WBR snapshot: "
        f"review_week={wbr_context['review_week']}, "
        f"window={wbr_context['window_start']} to {wbr_context['window_end']}, "
        f"source_file={wbr_context['source_file']}"
    )

    print("Syncing Notion pages...")
    created_count = 0
    updated_count = 0
    for row in csv_rows:
        interaction_date = clean_value(row[DATE_FIELD])
        unique_row_key = f"{wbr_context['review_week']}::{interaction_date}"

        properties = build_page_properties(row, title_property_name, wbr_context)
        existing_page = find_page_for_unique_row_key(unique_row_key)

        if existing_page:
            update_notion_page(existing_page["id"], properties)
            updated_count += 1
            print(f"  - Updated {unique_row_key}")
        else:
            create_notion_page(properties)
            created_count += 1
            print(f"  - Added {unique_row_key}")

    print("Syncing week-on-week chart views...")
    view_database = notion_request(
        "GET",
        f"/databases/{DATABASE_ID}",
        headers=VIEW_HEADERS,
    )
    data_source_id = get_data_source_id(view_database)
    created_charts, updated_charts = sync_chart_views(data_source_id, wbr_context)

    print("Syncing example WBR dashboard...")
    wbr_result = sync_wbr_example_views(data_source_id, csv_rows)

    print("Generating and pushing layered WBR combo chart...")
    combo_result = push_wbr_combo_chart_to_notion(csv_rows, title_property_name)
    print(f"  - Uploaded combo chart to page: {combo_result['report_title']}")
    print(f"  - Local chart image: {combo_result['image_path']}")

    print(
        "\nDone. "
        f"Added {created_count} rows and updated {updated_count} rows in Notion. "
        f"Created {created_charts} charts and updated {updated_charts} charts. "
        f"WBR latest week is {wbr_result['latest_date']} "
        f"vs {wbr_result['previous_date']}; "
        f"created {wbr_result['widgets_created']} WBR widgets and "
        f"updated {wbr_result['widgets_updated']} WBR widgets. "
        f"Pushed layered combo chart to {combo_result['report_title']}."
    )


if __name__ == "__main__":
    main()
