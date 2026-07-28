import os
import keepa
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(".env") if Path(".env").exists() else Path("../.env"))
api = keepa.Keepa(os.getenv("KEEPA_API_KEY"))
api.update_status()
print("Tokens:", api.tokens_left)

query_100 = {
    "productType": ["0"],
    "brand": ["philips"],
    "sort": [["current_SALES", "asc"], ["monthlySold", "desc"]],
    "perPage": 100,
    "page": 0
}

query_20 = {
    "productType": ["0"],
    "brand": ["philips"],
    "sort": [["current_SALES", "asc"], ["monthlySold", "desc"]],
    "perPage": 20,
    "page": 0
}

try:
    result = api.product_finder(query_100, wait=False)
    print("perPage=100: SUCCESS,", len(result), "results")
except Exception as e:
    print("perPage=100: FAILED,", repr(e))

try:
    result = api.product_finder(query_20, wait=False)
    print("perPage=20: SUCCESS,", len(result), "results")
except Exception as e:
    print("perPage=20: FAILED,", repr(e))