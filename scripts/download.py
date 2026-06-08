"""
NSFC Final Research Report Downloader.

Downloads report images from NSFC project detail pages and assembles them into
PDF files. Requires Chrome with CDP for auth token extraction.

Usage:
    python scripts/download.py [--task-list TASK_LIST_FILE_2]
"""
import os
import sys
import time
import argparse
from io import BytesIO
from datetime import datetime

import requests
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config_loader import (
    NSFC_HOST, DOWNLOAD_DIR, CDP_PORT, REQUEST_DELAY,
)
from lib.cdp_client import CDPClient
from lib.browser_launcher import wait_for_cdp, launch_chrome
from lib.downloader_lib import load_task_list, save_task_list

# ── Auth ──────────────────────────────────────────────────────────────────────

def extract_token(client):
    result = client.evaluate_js("localStorage.getItem('access')")
    return result.get("result", {}).get("value", "")


def create_session(token):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Authorization": f"Bearer {token}",
        "accept": "application/json, text/plain, */*",
        "content-type": "application/x-www-form-urlencoded",
        "origin": NSFC_HOST,
    })
    return s


# ── Download ──────────────────────────────────────────────────────────────────

def download_project(session, project_id, project_name):
    """Download report images and pack into PDF. Returns (status, error)."""
    info = None
    for attempt in range(3):
        try:
            resp = session.post(
                f"{NSFC_HOST}/api/baseQuery/conclusionProjectInfo/{project_id}",
                timeout=15)
            if resp.status_code == 503:
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code != 200 or not resp.text.strip():
                return "no_report", f"API status {resp.status_code}"
            info = resp.json().get("data", {})
            break
        except Exception:
            time.sleep(3)

    if not info or not isinstance(info, dict) or not info:
        return "no_report", "No project data"
    if not info.get("hasReport"):
        return "no_report", "No final report uploaded"

    name = info.get("projectName", project_name)
    safe_name = "".join(c for c in name if c not in r'\/:*?"<>|')
    pdf_path = os.path.join(DOWNLOAD_DIR, f"{safe_name}.pdf")

    if os.path.exists(pdf_path):
        return "success", ""

    images_data = []
    for pi in range(1, 200):
        url = None
        for retry in range(3):
            try:
                resp = session.post(
                    f"{NSFC_HOST}/api/baseQuery/completeProjectReport",
                    data={"id": project_id, "index": str(pi)}, timeout=15)
                if resp.status_code == 503:
                    time.sleep(5 * (retry + 1))
                    continue
                if resp.status_code != 200 or not resp.text.strip():
                    break
                url = resp.json().get("data", {}).get("url", "")
                break
            except Exception:
                time.sleep(2)
        if not url:
            break

        for retry2 in range(3):
            try:
                img_resp = session.get(NSFC_HOST + url, timeout=30)
                if img_resp.status_code == 503:
                    time.sleep(5 * (retry2 + 1))
                    continue
                break
            except Exception:
                time.sleep(2)
        if img_resp.status_code == 200 and len(img_resp.content) > 1024:
            images_data.append((img_resp.content, pi))
            if pi % 10 == 1 or pi <= 3:
                print(f"    Page {pi} OK ({len(img_resp.content)} bytes)")
        else:
            break
        time.sleep(REQUEST_DELAY)

    if not images_data:
        return "failed", "No images retrieved"

    images_data.sort(key=lambda x: x[1])
    pil_images = []
    for img_bytes, _ in images_data:
        try:
            pil_images.append(Image.open(BytesIO(img_bytes)))
        except Exception:
            pass
    if not pil_images:
        return "failed", "PDF generation failed"

    output = BytesIO()
    pil_images[0].save(output, "PDF", resolution=100.0, save_all=True,
                       append_images=pil_images[1:])
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    with open(pdf_path, "wb") as f:
        f.write(output.getvalue())
    return "success", ""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NSFC Final Report Downloader")
    parser.add_argument("--task-list", default=None,
                       help="Task list CSV path (default: task_list2.csv)")
    args = parser.parse_args()

    print("=" * 60)
    print("NSFC Final Report Downloader")
    print("=" * 60)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Determine task list file
    task_file = args.task_list or os.path.join(
        os.path.dirname(__file__), "..", "task_list2.csv")

    tasks = load_task_list(task_file)
    pending = [t for t in tasks if t["status"] == "pending"]
    print(f"Tasks: {len(tasks)} total, {len(pending)} pending")

    # Launch Chrome (needed for auth token)
    print("\n[1/4] Starting Chrome...")
    if not wait_for_cdp(timeout=2):
        launch_chrome()
        if not wait_for_cdp():
            print("Chrome failed to start!")
            sys.exit(1)
    else:
        print("  Chrome already running")

    # Connect & auth
    print("\n[2/4] Getting auth token...")
    client = CDPClient(CDP_PORT)
    client.connect()
    token = extract_token(client)
    if not token:
        print("\nPlease log in to your NSFC account in the browser...")
        print("Waiting for login...", end="", flush=True)
        for _ in range(120):
            time.sleep(2)
            token = extract_token(client)
            if token:
                break
            print(".", end="", flush=True)
        if not token:
            print("\nLogin timeout. Exiting.")
            sys.exit(1)
        print(" OK")
    print(f"Token: {token[:30]}...")
    session = create_session(token)
    client.close()
    print("  CDP closed")

    # Verify API
    print("  Testing API...", end="")
    resp = session.post(
        f"{NSFC_HOST}/api/baseQuery/conclusionProjectInfo/d2ba133ee2e6748c133d86bf52b1dd80",
        timeout=15)
    if resp.status_code != 200 or not resp.json().get("data"):
        print(" failed")
        sys.exit(1)
    print(" OK")

    # Download
    print(f"\n[3/4] Downloading reports...")
    pending = [t for t in tasks if t["status"] == "pending"]
    print(f"Pending: {len(pending)}")

    for i, task in enumerate(pending):
        try:
            print(f"\n[{i+1}/{len(pending)}] {task['name'][:40]}")
        except UnicodeEncodeError:
            print(f"\n[{i+1}/{len(pending)}] <unicode name>")

        try:
            # Refresh token periodically
            if i > 0 and i % 50 == 0:
                print("  Refreshing token...")
                client = CDPClient(CDP_PORT)
                client.connect()
                new_token = extract_token(client)
                if new_token:
                    session = create_session(new_token)
                client.close()
            status, error = download_project(session, task["id"], task["name"])
            task["status"] = status
            task["error"] = error
        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)
        task["updated_at"] = datetime.now().isoformat()
        save_task_list(tasks, task_file)
        if task["status"] in ("failed", "no_report"):
            print(f"  [{task['status']}] {task['error']}")
        time.sleep(REQUEST_DELAY)

    success = sum(1 for t in tasks if t["status"] == "success")
    failed = sum(1 for t in tasks if t["status"] == "failed")
    no_report = sum(1 for t in tasks if t["status"] == "no_report")
    print(f"\n{'='*60}")
    print(f"Complete! Success: {success}  Failed: {failed}  No-report: {no_report}  Total: {len(tasks)}")
    print(f"Directory: {DOWNLOAD_DIR}")


if __name__ == "__main__":
    main()
