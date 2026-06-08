"""
Shared search and download utilities for NSFC report automation.

Provides Vue state extraction, sidebar interaction, project parsing,
task management, and a reusable SearchRunner class.
"""
import csv
import json
import os
import time
import urllib.parse
from datetime import datetime

from .config_loader import NSFC_HOST, MAX_RESULTS_THRESHOLD

# ── Vue state extraction ──────────────────────────────────────────────────────

def get_vue_data(client):
    """Extract Vue component state (finalSearchList) from the page."""
    result = client.evaluate_js("""
    (() => {
        for (const div of document.querySelectorAll('div')) {
            const vm = div.__vueParentComponent?.proxy
                    || div.__vueParentComponent?.instance?.proxy
                    || div.__vue__
                    || null;
            if (vm && vm.$options && vm.$options.name === 'finalSearchList') {
                return JSON.stringify({
                    dataCount: vm.dataCount, pagesCount: vm.pagesCount,
                    currentPage: vm.currentPage, resultsData: vm.resultsData,
                });
            }
        }
        return '{}';
    })()
    """)
    return json.loads(result.get("result", {}).get("value", "{}"))


def get_sidebar_options(client):
    """Extract sidebar filter options from Vue component."""
    result = client.evaluate_js("""
    (() => {
        for (const div of document.querySelectorAll('div')) {
            const vm = div.__vueParentComponent?.proxy
                    || div.__vueParentComponent?.instance?.proxy
                    || div.__vue__
                    || null;
            if (vm && vm.$options && vm.$options.name === 'finalSearchList') {
                const out = {};
                vm.sidebarList.forEach((item, i) => {
                    const opts = [];
                    for (const opt of item.radioList) {
                        if (typeof opt === 'object' && opt !== null)
                            opts.push({value: opt.code || opt.name, label: opt.name});
                        else
                            opts.push({value: opt, label: opt});
                    }
                    out[i] = {title: item.title, options: opts};
                });
                return JSON.stringify(out);
            }
        }
        return '{}';
    })()
    """)
    return json.loads(result.get("result", {}).get("value", "{}"))


def click_sidebar_option(client, text):
    """Click a sidebar radio label by visible text."""
    js = f"""
    (() => {{
        const labels = document.querySelectorAll('.el-radio__label');
        for (const el of labels) {{
            if (el.textContent.trim() === '{text}') {{ el.click(); return 'ok'; }}
        }}
        return 'not-found';
    }})()
    """
    return client.evaluate_js(js).get("result", {}).get("value")


def goto_page(client, page_num):
    """Navigate to page N via Vue pageNum offset."""
    js = f"""
    (() => {{
        for (const div of document.querySelectorAll('div')) {{
            const vm = div.__vueParentComponent?.proxy
                    || div.__vueParentComponent?.instance?.proxy
                    || div.__vue__
                    || null;
            if (vm && vm.$options && vm.$options.name === 'finalSearchList') {{
                vm.pageNum = {(page_num - 1) * 10};
                vm.getResultsDataList(true);
                return 'done';
            }}
        }}
        return 'no';
    }})()
    """
    return client.evaluate_js(js).get("result", {}).get("value")


def wait_for_data_change(client, old_count, timeout=15):
    """Wait for dataCount to change after a filter click."""
    time.sleep(0.5)
    start = time.time()
    while time.time() - start < timeout:
        s = get_vue_data(client)
        dc = s.get("dataCount", 0)
        results = s.get("resultsData") or []
        if dc >= 0 and dc != old_count and len(results) > 0:
            return s
        if dc == 0 and len(results) == 0:
            time.sleep(0.5)
            s2 = get_vue_data(client)
            if s2.get("dataCount") == 0:
                return s2
            continue
        time.sleep(0.3)
    return get_vue_data(client)


# ── Project parsing ───────────────────────────────────────────────────────────

def parse_projects(results_data, keyword, filter_info):
    """Parse Vue results data into list of project dicts."""
    projects = []
    for item in results_data or []:
        if isinstance(item, list) and len(item) >= 2:
            projects.append({
                "id": item[0],
                "name": item[1],
                "approvalNo": item[2] if len(item) > 2 else "",
                "projectType": item[3] if len(item) > 3 else "",
                "unit": item[4] if len(item) > 4 else "",
                "keyword": keyword,
                "filter_info": filter_info,
            })
    return projects


# ── Task list management ──────────────────────────────────────────────────────

TASK_FIELDNAMES = ["id", "name", "keyword", "approvalNo", "status", "error", "updated_at", "filter_info"]


def load_task_list(filepath):
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_task_list(tasks, filepath):
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TASK_FIELDNAMES)
        writer.writeheader()
        for t in tasks:
            writer.writerow(t)


def merge_tasks(existing, new_projects):
    existing_ids = {t["id"] for t in existing}
    count = 0
    for proj in new_projects:
        if proj["id"] not in existing_ids:
            existing.append({
                "id": proj["id"],
                "name": proj["name"],
                "keyword": proj["keyword"],
                "approvalNo": proj.get("approvalNo", ""),
                "status": "pending",
                "error": "",
                "updated_at": datetime.now().isoformat(),
                "filter_info": proj.get("filter_info", ""),
            })
            existing_ids.add(proj["id"])
            count += 1
    return existing, count


def load_seen_ids(*filepaths):
    """Load all project IDs from existing task CSVs for deduplication."""
    seen = set()
    for fp in filepaths:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    seen.add(row["id"])
    return seen


# ── SearchRunner ──────────────────────────────────────────────────────────────

class SearchRunner:
    """Reusable multi-dimensional search with layered filtering.

    Handles the NSFC site's top-100 result limitation by progressively
    narrowing search scope using sidebar filters (year, category, discipline,
    institution).
    """

    def __init__(self, client, seen_ids, max_pages=10, skip_values=None):
        self.client = client
        self.seen_ids = seen_ids
        self.max_pages = max_pages
        self.skip_values = skip_values or {"近五年"}

    def navigate_and_wait(self, keyword):
        """Navigate to search results page and wait for Vue SPA to render."""
        kw = urllib.parse.quote(keyword)
        self.client.navigate(f"{NSFC_HOST}/finalSearchList?s={kw}")
        self.client.wait_for_load(timeout=15)
        time.sleep(5)
        for _ in range(25):
            if get_vue_data(self.client).get("dataCount", 0) > 0:
                break
            time.sleep(1)

    def fresh_search(self, keyword, filters, filter_info, full_count=None, retry=True):
        """Execute a search with optional sidebar filters and collect results."""
        collected = []
        self.navigate_and_wait(keyword)
        s = get_vue_data(self.client)

        for click_text in filters:
            old = s
            click_sidebar_option(self.client, click_text)
            s = wait_for_data_change(self.client, old.get("dataCount", -1))
            time.sleep(1.0)

        total = s.get("dataCount", 0)
        if total == 0:
            return collected, total

        if retry and full_count and len(filters) > 0 and total >= full_count:
            print(f"        Retry: {filter_info}")
            return self.fresh_search(keyword, filters, filter_info, full_count, retry=False)

        calc_pages = (total + 9) // 10
        pgs = min(calc_pages, self.max_pages)

        prev_ids = None
        projects = parse_projects(s.get("resultsData"), keyword, filter_info)
        if projects:
            prev_ids = {p["id"] for p in projects}
        for p in projects:
            if p["id"] not in self.seen_ids:
                self.seen_ids.add(p["id"])
                collected.append(p)

        for pg in range(2, pgs + 1):
            time.sleep(1.5)
            goto_page(self.client, pg)
            time.sleep(1.5)
            s = get_vue_data(self.client)
            projects = parse_projects(s.get("resultsData"), keyword, filter_info)

            cur_ids = {p["id"] for p in projects} if projects else set()
            if not projects or cur_ids == prev_ids:
                time.sleep(2)
                s = get_vue_data(self.client)
                projects = parse_projects(s.get("resultsData"), keyword, filter_info)

            for p in projects:
                if p["id"] not in self.seen_ids:
                    self.seen_ids.add(p["id"])
                    collected.append(p)
            if projects:
                prev_ids = {p["id"] for p in projects}

        return collected, total

    def split_by_year_category(self, keyword, full_count, year_opts, category_opts):
        """Split a broad search by year, then by category if needed."""
        all_new = []
        usable_years = [o for o in year_opts
                       if o.get('label', o.get('value', '')) not in self.skip_values]

        for yr_opt in usable_years:
            yr_label = yr_opt.get("label", yr_opt.get("value", ""))
            projects, yr_total = self.fresh_search(
                keyword, [yr_label], f"{keyword}/{yr_label}", full_count)
            if projects:
                print(f"    year={yr_label}: {yr_total} results, collected {len(projects)}")
            all_new.extend(projects)
            time.sleep(0.5)

            if yr_total == 0 or yr_total >= full_count:
                continue

            if yr_total > MAX_RESULTS_THRESHOLD:
                usable_cats = [o for o in category_opts
                             if o.get('label', o.get('value', '')) not in self.skip_values]
                if usable_cats:
                    print(f"      splitting by category ({yr_total} results)...")
                    for cat_opt in usable_cats:
                        cat_label = cat_opt.get("label", cat_opt.get("value", ""))
                        projects, cat_total = self.fresh_search(
                            keyword, [yr_label, cat_label],
                            f"{keyword}/{yr_label}/{cat_label}", full_count)
                        if 0 < cat_total < full_count and projects:
                            print(f"        category={cat_label}: {cat_total} results, collected {len(projects)}")
                            all_new.extend(projects)
                        time.sleep(0.3)

        return all_new
