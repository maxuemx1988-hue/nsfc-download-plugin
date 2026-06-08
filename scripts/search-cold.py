"""
Supplemental cold/long-tail keyword search for NSFC projects.

Usage:
    python scripts/search-cold.py
    # Uses COLD_KEYWORDS from config.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config_loader import COLD_KEYWORDS, NSFC_HOST, CDP_PORT
from lib.cdp_client import CDPClient
from lib.browser_launcher import wait_for_cdp, launch_chrome
from lib.downloader_lib import (
    get_vue_data, get_sidebar_options, click_sidebar_option,
    goto_page, wait_for_data_change, parse_projects,
    load_seen_ids, merge_tasks, load_task_list, save_task_list,
)

SKIP_VALUES = {"近五年"}
MAX_PAGES = 10

TASK_LIST_FILE = os.path.join(os.path.dirname(__file__), "..", "task_list.csv")
TASK_LIST_FILE_2 = os.path.join(os.path.dirname(__file__), "..", "task_list2.csv")


def search_cold_keywords(client):
    if not COLD_KEYWORDS:
        print("No COLD_KEYWORDS configured in config.py.")
        return []

    seen_ids = load_seen_ids(TASK_LIST_FILE, TASK_LIST_FILE_2)
    all_new = []

    def _navigate_and_wait(keyword):
        import urllib.parse, time
        kw = urllib.parse.quote(keyword)
        client.navigate(f"{NSFC_HOST}/finalSearchList?s={kw}")
        client.wait_for_load(timeout=15)
        time.sleep(5)
        for _ in range(25):
            if get_vue_data(client).get("dataCount", 0) > 0:
                break
            time.sleep(1)

    def _fresh_search(keyword, filters, filter_info, full_count=None):
        nonlocal seen_ids
        import time
        collected = []
        _navigate_and_wait(keyword)
        s = get_vue_data(client)
        for click_text in filters:
            old = s
            click_sidebar_option(client, click_text)
            s = wait_for_data_change(client, old.get("dataCount", -1))
            time.sleep(1.0)
        total = s.get("dataCount", 0)
        if total == 0:
            return collected, total
        if full_count and len(filters) > 0 and total >= full_count:
            return _fresh_search(keyword, filters, filter_info, full_count)
        calc_pages = (total + 9) // 10
        pgs = min(calc_pages, MAX_PAGES)
        prev_ids = None
        projects = parse_projects(s.get("resultsData"), keyword, filter_info)
        if projects:
            prev_ids = {p["id"] for p in projects}
        for p in projects:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                collected.append(p)
        for pg in range(2, pgs + 1):
            time.sleep(1.5)
            goto_page(client, pg)
            time.sleep(1.5)
            s = get_vue_data(client)
            projects = parse_projects(s.get("resultsData"), keyword, filter_info)
            cur_ids = {p["id"] for p in projects} if projects else set()
            if not projects or cur_ids == prev_ids:
                time.sleep(2)
                s = get_vue_data(client)
                projects = parse_projects(s.get("resultsData"), keyword, filter_info)
            for p in projects:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    collected.append(p)
            if projects:
                prev_ids = {p["id"] for p in projects}
        return collected, total

    _navigate_and_wait(COLD_KEYWORDS[0])
    sidebar = get_sidebar_options(client)
    year_opts = sidebar.get("0", {}).get("options", [])
    print(f"  year: {[o['label'] for o in year_opts]}")

    import time
    for kw in COLD_KEYWORDS:
        print(f"\n{'='*50}")
        print(f"Keyword: {kw}")
        projects, total = _fresh_search(kw, [], f"{kw}")
        full_count = total
        print(f"  {kw}: {total} results, collected {len(projects)} (new)")
        all_new.extend(projects)
        time.sleep(0.5)

        if total > 90 and total < 900:
            for yr_opt in year_opts:
                yr_label = yr_opt.get("label", yr_opt.get("value", ""))
                if yr_label in SKIP_VALUES:
                    continue
                projects, yr_total = _fresh_search(
                    kw, [yr_label], f"{kw}/{yr_label}", full_count)
                print(f"    year={yr_label}: {yr_total} results, collected {len(projects)} (new)")
                all_new.extend(projects)
                time.sleep(0.5)

    return all_new


def main():
    print("=" * 60)
    print("NSFC Search - Cold Keywords")
    print(f"Keywords: {COLD_KEYWORDS}")
    print("=" * 60)

    print("\nConnecting Chrome...")
    if not wait_for_cdp(timeout=2):
        launch_chrome()
        if not wait_for_cdp():
            print("Chrome failed to start!")
            sys.exit(1)
    else:
        print("  Chrome already running")

    client = CDPClient(CDP_PORT)
    client.connect()

    projects = search_cold_keywords(client)
    print(f"\n{'='*60}")
    print(f"Found {len(projects)} new projects")

    tasks = load_task_list(TASK_LIST_FILE_2)
    tasks, new_count = merge_tasks(tasks, projects)
    save_task_list(tasks, TASK_LIST_FILE_2)
    print(f"task_list2: {len(tasks)} projects ({new_count} new)")
    client.close()


if __name__ == "__main__":
    main()
