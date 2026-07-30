"""Google Driveのアップロードフォルダを確認し、新しい契約書・精算書の画像を
Claude(vision)で読み取ってdata/records.jsonに追記する。

GitHub Actionsから定期実行される想定(.github/workflows/process_uploads.yml)。
処理済みのファイルIDはrecords.json内のprocessed_file_idsに記録し、
二重登録を防ぐ。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import anthropic

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = BASE_DIR / "data" / "records.json"

JST = timezone(timedelta(hours=9))
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

EXTRACTION_PROMPT = """あなたは中古車販売店の経理担当です。
添付の画像は自動車の売買契約書または精算書です。
次のJSON形式で情報を抽出してください。読み取れない項目はnullにしてください。
金額は数値(円、カンマなし)のみで出力してください。

{
  "deal_date": "YYYY-MM-DD形式の取引日",
  "type": "buy(仕入れ・買取)かsell(販売)のどちらか",
  "counterparty": "相手方の名前",
  "vehicle": "車名・型式",
  "chassis_no": "車台番号(あれば、なければ空文字)",
  "total_amount": 総額(円、数値),
  "recycle_fee": リサイクル料金(円、数値、なければ0),
  "auto_tax_proration": 自動車税・軽自動車税の月割額(円、数値、なければ0),
  "jibaiseki_fee": 自賠責保険料(円、数値、なければ0),
  "other_nontaxable_amount": 上記以外の非課税項目の合計(円、数値、なければ0),
  "other_nontaxable_note": "その他非課税項目の内容(印紙代など。なければ空文字)"
}

JSON以外の文字列は一切出力しないでください。
"""


def _load_data() -> Dict[str, Any]:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def _save_data(data: Dict[str, Any]) -> None:
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _drive_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _list_images(service, folder_id: str) -> List[Dict[str, Any]]:
    query = f"'{folder_id}' in parents and (mimeType contains 'image/') and trashed = false"
    files: List[Dict[str, Any]] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def _download_image(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _extract_fields(image_bytes: bytes, mime_type: str) -> Optional[Dict[str, Any]]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    b64 = base64.b64encode(image_bytes).decode("ascii")
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group(0))


def _compute_derived(fields: Dict[str, Any]) -> Dict[str, Any]:
    total = fields.get("total_amount") or 0
    nontax = (
        (fields.get("recycle_fee") or 0)
        + (fields.get("auto_tax_proration") or 0)
        + (fields.get("jibaiseki_fee") or 0)
        + (fields.get("other_nontaxable_amount") or 0)
    )
    taxable = max(total - nontax, 0)
    # 総額は税込表記が通例のため、課税対象額(税込)から消費税額(10%)を逆算する
    tax_estimate = round(taxable * 10 / 110)
    return {"taxable_amount": taxable, "consumption_tax_estimate": tax_estimate}


def main() -> None:
    data = _load_data()
    service = _drive_service()
    folder_id = data["upload_folder_id"]
    processed = set(data.get("processed_file_ids", []))

    files = _list_images(service, folder_id)
    new_files = [f for f in files if f["id"] not in processed]

    if not new_files:
        print("新しい画像はありません")
        return

    next_no = max((r["no"] for r in data["records"]), default=0) + 1

    for f in new_files:
        print(f"処理中: {f['name']} ({f['id']})")
        try:
            image_bytes = _download_image(service, f["id"])
            fields = _extract_fields(image_bytes, f.get("mimeType", "image/jpeg"))
        except Exception as exc:  # noqa: BLE001
            print(f"  読み取りに失敗しました: {exc}")
            continue

        if fields is None:
            print("  JSON抽出に失敗しました(スキップ)")
            continue

        derived = _compute_derived(fields)
        record = {
            "no": next_no,
            "recorded_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
            "deal_date": fields.get("deal_date"),
            "type": fields.get("type"),
            "counterparty": fields.get("counterparty"),
            "vehicle": fields.get("vehicle"),
            "chassis_no": fields.get("chassis_no"),
            "total_amount": fields.get("total_amount") or 0,
            "recycle_fee": fields.get("recycle_fee") or 0,
            "auto_tax_proration": fields.get("auto_tax_proration") or 0,
            "jibaiseki_fee": fields.get("jibaiseki_fee") or 0,
            "other_nontaxable_amount": fields.get("other_nontaxable_amount") or 0,
            "other_nontaxable_note": fields.get("other_nontaxable_note") or "",
            "taxable_amount": derived["taxable_amount"],
            "consumption_tax_estimate": derived["consumption_tax_estimate"],
            "source_file": f["name"],
            "note": "",
        }
        data["records"].append(record)
        data["processed_file_ids"].append(f["id"])
        next_no += 1

    _save_data(data)
    print(f"合計 {len(data['records'])} 件のデータになりました")


if __name__ == "__main__":
    main()
