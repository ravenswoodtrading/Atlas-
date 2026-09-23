"""
Writes the Keepa watch lists (see app/services/keepa_watch_list_service.py) to ../exports/keepa_lists/.

    cd backend
    PYTHONPATH=. python export_keepa_watch_lists.py

Read-only: no Keepa or SP-API calls, no database writes.
"""
import os

from app.services.keepa_watch_list_service import KeepaWatchListService

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports", "keepa_lists")

if __name__ == "__main__":
    result = KeepaWatchListService.build()
    s = result["summary"]
    print(f"{s['candidates']} candidates -> {s['included']} on the lists, {s['excluded']} left out")
    print("left out because:")
    for reason, n in s["excluded_by_reason"].items():
        print(f"   {n:>4}  {reason}")
    print("EU-drop lists  :", {f"{b}%": n for b, n in s["eu_lists"].items()})
    print("UK-rise lists  :", {f"{b}%": n for b, n in s["uk_lists"].items()})
    for label, path in KeepaWatchListService.write_files(result, OUT_DIR).items():
        print("wrote", path)
