import pandas as pd

def safe_date(v):
    if v is None or v == "":
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    try:
        return str(pd.to_datetime(v).date())
    except Exception:
        return None

print("safe_date int 20230715:", safe_date(20230715))
print("safe_date str 20230715:", safe_date("20230715"))
print("safe_date float 20230715.0:", safe_date(20230715.0))
print("safe_date int 0:", safe_date(0))
print("safe_date str 0:", safe_date("0"))
print("safe_date None:", safe_date(None))
