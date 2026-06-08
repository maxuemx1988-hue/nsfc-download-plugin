"""
Search NSFC projects by institution dimension (institution -> year -> keyword).

Usage:
    python scripts/search-institution.py [keyword]
    # Default: first KEYWORDS entry
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config_loader import KEYWORDS, NSFC_HOST, CDP_PORT, MAX_RESULTS_THRESHOLD
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


def search_by_institution(client, keyword):
    seen_ids = load_seen_ids(TASK_LIST_FILE, TASK_LIST_FILE_2)
    all_new = []
    full_count = 0

    def _navigate():
        import urllib.parse, time
        kw = urllib.parse.quote(keyword)
        client.navigate(f"{NSFC_HOST}/finalSearchList?s={kw}")
        client.wait_for_load(timeout=15)
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
    inst_opts = sidebar.get("4", {}).get("options", [])
    year_opts = sidebar.get("0", {}).get("options", [])
    kw_opts = sidebar.get("2", {}).get("options", [])
    print(f"  institution: {[o['label'] for o in inst_opts]}")
    print(f"  year: {[o['label'] for o in year_opts]}")
    print(f"  keywords: {[o['label'] for o in kw_opts]}")

    projects, total = _fresh_search([], f"{keyword}")
    full_count = total
    print(f"  {keyword}: {total} results, collected {len(projects)} (new)")
    all_new.extend(projects)

    if total == 0:
        return all_new

    import time
    usable_inst = [o for o in inst_opts
                   if o.get('label', o.get('value', '')) not in SKIP_VALUES]
    for inst_opt in usable_inst:
        inst_label = inst_opt.get("label", inst_opt.get("value", ""))
        projects, inst_total = _fresh_search(
            [inst_label], f"{keyword}/{inst_label}")
        print(f"    inst={inst_label}: {inst_total} results, collected {len(projects)} (new)")
        all_new.extend(projects)
        time.sleep(0.5)

        if inst_total == 0 or inst_total >= full_count:
            continue

        if inst_total > MAX_RESULTS_THRESHOLD:
            for yr_opt in year_opts:
                yr_label = yr_opt.get("label", yr_opt.get("value", ""))
                if yr_label in SKIP_VALUES:
                    continue
                projects, yr_total = _fresh_search(
                    [inst_label, yr_label],
                    f"{keyword}/{inst_label}/{yr_label}")
                print(f"      year={yr_label}: {yr_total} results, collected {len(projects)} (new)")
                all_new.extend(projects)
                time.sleep(0.5)

                if yr_total == 0 or yr_total >= full_count:
                    continue

                if yr_total > MAX_RESULTS_THRESHOLD:
                    usable_kw = [o for o in kw_opts
                                if o.get('label', o.get('value', '')) not in SKIP_VALUES]
                    if usable_kw:
                        print(f"        splitting by keyword ({yr_total} results)...")
                        for kw_opt in usable_kw:
                            kw_label = kw_opt.get("label", kw_opt.get("value", ""))
                            projects, kw_total = _fresh_search(
                                [inst_label, yr_label, kw_label],
                                f"{keyword}/{inst_label}/{yr_label}/{kw_label}")
                            if 0 < kw_total < full_count:
                                print(f"          kw={kw_label}: {kw_total} results, collected {len(projects)} (new)")
                                all_new.extend(projects)
                            time.sleep(0.3)

    return all_new


def main():
    import argparse
    parser = argparse.ArgumentParser(description="NSFC search by institution dimension")
    parser.add_argument("keyword", nargs="?", default=None,
                       help="Keyword to search (default: first KEYWORDS entry)")
    args = parser.parse_args()

    keyword = args.keyword or (KEYWORDS[0] if KEYWORDS else None)
    if not keyword:
        print("No keyword specified.")
        sys.exit(1)

    print("=" * 60)
    print("NSFC Search - Institution Dimension")
    print(f"Keyword: {keyword}")
    print("=" * 60)

    print("\n[1/3] Starting Chrome...")
    if not wait_for_cdp(timeout=2):
        launch_chrome()
        if not wait_for_cdp():
            print("Chrome failed to start!")
            sys.exit(1)
    else:
        print("  Chrome already running")

    print("\n[2/3] Connecting CDP...")
    client = CDPClient(CDP_PORT)
    client.connect()

    print(f"\n[3/3] Searching (institution -> year -> keyword)...")
    print(f"\n{'='*50}")
    print(f"Keyword: {keyword}")
    print(f"{'='*50}")
    projects = search_by_institution(client, keyword)
    print(f"\n  Found {len(projects)} new projects")

    tasks = load_task_list(TASK_LIST_FILE_2)
    tasks, new_count = merge_tasks(tasks, projects)
    save_task_list(tasks, TASK_LIST_FILE_2)
    print(f"\ntask_list2: {len(tasks)} projects ({new_count} new)")
    client.close()


if __name__ == "__main__":
    main()
