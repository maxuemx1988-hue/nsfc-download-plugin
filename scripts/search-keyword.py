"""
Search NSFC projects by main keywords with year/category/discipline splitting.

Usage:
    python scripts/search-keyword.py [keyword]  # Default: first KEYWORDS entry
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config_loader import KEYWORDS, NSFC_HOST, CDP_PORT
from lib.cdp_client import CDPClient
from lib.browser_launcher import wait_for_cdp, launch_chrome
from lib.downloader_lib import (
    get_vue_data, get_sidebar_options, click_sidebar_option,
    goto_page, wait_for_data_change, parse_projects,
    load_seen_ids, merge_tasks, load_task_list, save_task_list,
    TASK_FIELDNAMES, MAX_RESULTS_THRESHOLD
)

SKIP_VALUES = {"近五年"}
MAX_PAGES = 10

TASK_LIST_FILE = os.path.join(os.path.dirname(__file__), "..", "task_list.csv")
TASK_LIST_FILE_2 = os.path.join(os.path.dirname(__file__), "..", "task_list2.csv")


def search_keyword(client, keyword):
    seen_ids = load_seen_ids(TASK_LIST_FILE, TASK_LIST_FILE_2)
    all_new = []
    full_count = 0

    def _navigate():
        import urllib.parse
        kw = urllib.parse.quote(keyword)
        client.navigate(f"{NSFC_HOST}/finalSearchList?s={kw}")
        client.wait_for_load(timeout=15)
        import time
        time.sleep(5)
        for _ in range(25):
            if get_vue_data(client).get("dataCount", 0) > 0:
                break
            time.sleep(1)

    def _fresh_search(filters, filter_info, retry=True):
        nonlocal full_count
        import time
        collected = []
        _navigate()
        s = get_vue_data(client)
        for click_text in filters:
            old = s
            click_sidebar_option(client, click_text)
            s = wait_for_data_change(client, old.get("dataCount", -1))
            time.sleep(1.0)
        total = s.get("dataCount", 0)
        if total == 0:
            return collected, total
        if retry and full_count > 0 and len(filters) > 0 and total >= full_count:
            print(f"        Retry: {filter_info}")
            return _fresh_search(filters, filter_info, retry=False)
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

    _navigate()
    sidebar = get_sidebar_options(client)
    year_opts = sidebar.get("0", {}).get("options", [])
    category_opts = sidebar.get("1", {}).get("options", [])
    discipline_opts = sidebar.get("3", {}).get("options", [])
    print(f"  year: {[o['label'] for o in year_opts]}")
    print(f"  category: {[o['label'] for o in category_opts]}")
    print(f"  discipline: {[o['label'] for o in discipline_opts]}")

    projects, total = _fresh_search([], f"{keyword}")
    full_count = total
    print(f"  {keyword}: {total} results, collected {len(projects)}")
    all_new.extend(projects)

    if total == 0:
        return all_new

    for year_opt in year_opts:
        year_label = year_opt.get("label", year_opt.get("value", ""))
        if year_label in SKIP_VALUES:
            continue
        projects, year_total = _fresh_search([year_label], f"{keyword}/{year_label}")
        print(f"    year={year_label}: {year_total} results, collected {len(projects)}")
        all_new.extend(projects)
        import time
        time.sleep(0.5)

        if year_total == 0 or year_total >= full_count:
            continue

        for cat_opt in category_opts:
            cat_label = cat_opt.get("label", cat_opt.get("value", ""))
            projects, cat_total = _fresh_search(
                [year_label, cat_label], f"{keyword}/{year_label}/{cat_label}")
            print(f"      category={cat_label}: {cat_total} results, collected {len(projects)}")
            all_new.extend(projects)
            time.sleep(0.5)

            if cat_total > MAX_RESULTS_THRESHOLD and cat_total < full_count:
                usable = [o for o in discipline_opts
                         if o.get('label', o.get('value', '')) not in SKIP_VALUES]
                if usable:
                    print(f"        splitting by discipline ({cat_total} results)...")
                    for disc_opt in usable:
                        disc_label = disc_opt.get("label", disc_opt.get("value", ""))
                        projects, disc_total = _fresh_search(
                            [year_label, cat_label, disc_label],
                            f"{keyword}/{year_label}/{cat_label}/{disc_label}")
                        if 0 < disc_total < full_count:
                            print(f"          discipline={disc_label}: {disc_total} results, collected {len(projects)}")
                            all_new.extend(projects)
                        time.sleep(0.3)

    return all_new


def main():
    import argparse
    parser = argparse.ArgumentParser(description="NSFC search by main keyword")
    parser.add_argument("keyword", nargs="?", default=None,
                       help="Keyword to search (default: first KEYWORDS entry)")
    args = parser.parse_args()

    keyword = args.keyword or (KEYWORDS[0] if KEYWORDS else None)
    if not keyword:
        print("No keyword specified. Set KEYWORDS in config.py or pass as argument.")
        sys.exit(1)

    print("=" * 60)
    print("NSFC Search - Main Keyword")
    print(f"Keyword: {keyword}")
    print("=" * 60)

    print("\n[1/2] Connecting Chrome...")
    if not wait_for_cdp(timeout=2):
        launch_chrome()
        if not wait_for_cdp():
            print("Chrome failed to start!")
            sys.exit(1)
    else:
        print("  Chrome already running")

    client = CDPClient(CDP_PORT)
    client.connect()

    print(f"\n[2/2] Searching (keyword -> year -> category -> discipline)...")
    projects = search_keyword(client, keyword)
    print(f"\n  Found {len(projects)} new projects")

    tasks = load_task_list(TASK_LIST_FILE_2)
    tasks, new_count = merge_tasks(tasks, projects)
    save_task_list(tasks, TASK_LIST_FILE_2)
    print(f"task_list2: {len(tasks)} projects ({new_count} new)")
    client.close()


if __name__ == "__main__":
    main()
