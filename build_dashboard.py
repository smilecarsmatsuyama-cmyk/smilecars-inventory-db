"""data/records.json からダッシュボード(docs/index.html)を生成する。

GitHub Actions(scripts/process_uploads.py)が新しい契約書データを
records.jsonに追記した後に実行され、GitHub Pages公開用のdocs/を更新する。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "data" / "records.json"
TEMPLATE_PATH = BASE_DIR / "templates" / "dashboard_template.html"
OUTPUT_PATH = BASE_DIR / "docs" / "index.html"

JST = timezone(timedelta(hours=9))


def main() -> None:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    generated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    records_json = json.dumps(data, ensure_ascii=False)

    html = (
        template
        .replace("__RECORDS_JSON__", records_json)
        .replace("__GENERATED_AT__", generated_at)
        .replace("__UPLOAD_FOLDER_URL__", data["upload_folder_url"])
    )
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"generated: {OUTPUT_PATH} ({len(data['records'])} 件)")


if __name__ == "__main__":
    main()
