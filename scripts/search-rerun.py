"""
Retry search for keywords that returned 0 results (browser state degraded).

Usage:
    python scripts/search-rerun.py
    # Uses RERUN_KEYWORDS from config.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config_loader import RERUN_KEYWORDS, NSFC_HOST, CDP_PORT
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


def search_rerun(client):
    if not RERUN_KEYWORDS:
        print("No RERUN_KEYWORDS configured in config.py.")
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

    _navigate_and_wait(RERUN_KEYWORDS[0])
    sidebar = get_sidebar_options(client)
    year_opts = sidebar.get("0", {}).get("options", [])
    category_opts = sidebar.get("1", {}).get("options", [])
    print(f"  year: {[o['label'] for o in year_opts]}")
    print(f"  category: {[o['label'] for o in category_opts]}")

    import time
    for kw in RERUN_KEYWORDS:
        print(f"\n{'='*50}")
        print(f"Sub-keyword: {kw}")
        projects, total = _fresh_search(kw, [], f"{kw}")
        full_count = total
        print(f"  {kw}: {total} results, collected {len(projects)} (new)")
        all_new.extend(projects)
        time.sleep(0.5)

        if total == 0 or total >= 900:
            continue

        if total > 90:
            usable_years = [o for o in year_opts
                          if o.get('label', o.get('value', '')) not in SKIP_VALUES]
            for yr_opt in usable_years:
                yr_label = yr_opt.get("label", yr_opt.get("value", ""))
                projects, yr_total = _fresh_search(
                    kw, [yr_label], f"{kw}/{yr_label}", full_count)
                print(f"    year={yr_label}: {yr_total} results, collected {len(projects)} (new)")
                all_new.extend(projects)
                time.sleep(0.5)

                if yr_total > 90 and yr_total < full_count:
                    usable_cats = [o for o in category_opts
                                  if o.get('label', o.get('value', '')) not in SKIP_VALUES]
                    if usable_cats:
                        print(f"      splitting by category ({yr_total} results)...")
                        for cat_opt in usable_cats:
                            cat_label = cat_opt.get("label", cat_opt.get("value", ""))
                            projects, cat_total = _fresh_search(
                                kw, [yr_label, cat_label],
                                f"{kw}/{yr_label}/{cat_label}", full_count)
                            if 0 < cat_total < full_count:
                                print(f"        category={cat_label}: {cat_total} results, collected {len(projects)} (new)")
                                all_new.extend(projects)
                            time.sleep(0.3)

    return all_new


def main():
    print("=" * 60)
    print("NSFC Rerun Search")
    print(f"Keywords: {RERUN_KEYWORDS}")
    print("=" * 60)

    # Restart Chrome to clear degraded browser state
    import subprocess, time
    print("\nRestarting Chrome...")
    subprocess.run("taskkill /f /im chrome.exe 2>nul", shell=True)
    time.sleep(3)
    launch_chrome()
    if not wait_for_cdp(timeout=30):
        print("Chrome failed to start!")
        sys.exit(1)

    client = CDPClient(CDP_PORT)
    client.connect()

    projects = search_rerun(client)
    print(f"\n{'='*60}")
    print(f"Rerun found {len(projects)} new projects")

    tasks = load_task_list(TASK_LIST_FILE_2)
    tasks, new_count = merge_tasks(tasks, projects)
    save_task_list(tasks, TASK_LIST_FILE_2)
    print(f"task_list2: {len(tasks)} projects ({new_count} new)")
    client.close()


if __name__ == "__main__":
    main()
