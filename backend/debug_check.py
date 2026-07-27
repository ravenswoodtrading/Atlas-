from app.services.product_service import ProductService
import json

products = ProductService().get_products(["B0FKJJGSSR"], "DE")
p = products[0]

print("=== csv[18] (buy box series) ===")
print(p.get("csv")[18])

print("\n=== stats ===")
print(json.dumps(p.get("stats"), indent=2, default=str))