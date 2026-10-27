import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from auth_pkce import (
    CLIENT_ID,
    CLIENT_SECRET,
    AUTH_HOST,
    API_HOST,
    TOKEN_FILE,
)

# Configuration
BASE_DIR = Path(__file__).resolve().parent
ARCHIVE_FILE = BASE_DIR / "mymojo_historical_readings.csv"
TABLEAU_FILE = BASE_DIR / "mymojo_tableau_ready.csv"
TOKEN_PATH = BASE_DIR / TOKEN_FILE

API_URL = f"https://{API_HOST}/api/v1/readings"
TOKEN_URL = f"https://{AUTH_HOST}/api/v1/oauth/token"

REFRESH_DAYS = 30
PAGE_SIZE = 5000
GLUCOSE_CONVERSION = 18.0182


# Authentication

def load_tokens():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Missing {TOKEN_PATH.name}. Run auth_pkce.py first."
        )

    with open(TOKEN_PATH, "r") as f:
        return json.load(f)


def save_tokens(tokens):
    temp_file = TOKEN_PATH.with_suffix(".tmp")

    with open(temp_file, "w") as f:
        json.dump(tokens, f, indent=2)

    temp_file.replace(TOKEN_PATH)


def refresh_access_token(tokens):
    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        raise RuntimeError(
            "No refresh token available. Run auth_pkce.py again."
        )

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        timeout=60,
    )
    response.raise_for_status()

    new_tokens = response.json()

    if not new_tokens.get("refresh_token"):
        new_tokens["refresh_token"] = refresh_token

    save_tokens(new_tokens)
    return new_tokens


def api_get(params, tokens):
    token = tokens.get("access_token") or tokens.get("token")

    if not token:
        raise RuntimeError("No access token found in tokens.json.")

    response = requests.get(
        API_URL,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=120,
    )

    if response.status_code == 401:
        tokens = refresh_access_token(tokens)
        token = tokens.get("access_token") or tokens.get("token")

        response = requests.get(
            API_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=120,
        )

    response.raise_for_status()
    return response.json(), tokens


# API response handling

def extract_rows(payload):
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in ("readings", "data", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value

        for value in payload.values():
            if isinstance(value, list) and (
                not value
                or (
                    isinstance(value[0], dict)
                    and "reading_timestamp" in value[0]
                )
            ):
                return value

    raise RuntimeError(
        "Could not identify readings in the API response."
    )


# Download recent readings

def fetch_window(start, end, tokens):
    params = {
        "from_date": start.isoformat().replace("+00:00", "Z"),
        "to_date": end.isoformat().replace("+00:00", "Z"),
        "page_size": PAGE_SIZE,
        "sort_order": "asc",
    }

    payload, tokens = api_get(params, tokens)
    rows = extract_rows(payload)

    # Split large time windows to avoid truncated downloads.
    if len(rows) >= PAGE_SIZE:
        midpoint = start + (end - start) / 2

        if midpoint <= start or midpoint >= end:
            raise RuntimeError(
                "API result reached the limit for a very small "
                "time window. Check API pagination settings."
            )

        print(f"Splitting large window: {start} to {end}")

        first, tokens = fetch_window(start, midpoint, tokens)
        second, tokens = fetch_window(midpoint, end, tokens)

        return first + second, tokens

    return rows, tokens


def fetch_recent_readings(tokens):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=REFRESH_DAYS)

    print(f"Fetching readings from {start} through {end}...")

    rows, tokens = fetch_window(start, end, tokens)

    if any("reading_id" not in row for row in rows):
        raise RuntimeError(
            "An API record has no reading_id. Stopping to avoid "
            "an unsafe merge."
        )

    print(f"Fetched {len(rows)} records.")
    return rows, tokens


# Compare values safely

def is_missing(value):
    if value is None:
        return True

    if isinstance(value, (list, dict)):
        return False

    check = pd.isna(value)

    if hasattr(check, "__len__"):
        return False

    return bool(check)


# Merge by reading_id

def merge_readings(existing_df, incoming_rows):
    if "reading_id" not in existing_df.columns:
        raise RuntimeError("Archive has no reading_id column.")

    if existing_df["reading_id"].isna().any():
        raise RuntimeError("Archive contains missing reading IDs.")

    incoming_df = pd.DataFrame(incoming_rows)

    if incoming_df.empty:
        return existing_df, 0, 0, 0

    if "reading_id" not in incoming_df.columns:
        raise RuntimeError("API data has no reading_id column.")

    if incoming_df["reading_id"].isna().any():
        raise RuntimeError("API returned records with missing IDs.")

    repeated_api_ids = int(
        incoming_df["reading_id"].duplicated(keep="last").sum()
    )

    incoming_df = incoming_df.drop_duplicates(
        subset=["reading_id"], keep="last"
    ).copy()

    existing_df = existing_df.copy()
    existing_df["reading_id"] = existing_df["reading_id"].astype(str)
    incoming_df["reading_id"] = incoming_df["reading_id"].astype(str)

    existing_df = existing_df.drop_duplicates(
        subset=["reading_id"], keep="last"
    )

    records = existing_df.set_index("reading_id").to_dict(
        orient="index"
    )

    original_ids = set(records)
    incoming_ids = set(incoming_df["reading_id"])

    new_count = len(incoming_ids - original_ids)
    updated_count = 0

    for row in incoming_df.to_dict(orient="records"):
        reading_id = row.pop("reading_id")
        old = records.get(reading_id)

        if old is None:
            records[reading_id] = {
                **row,
                "reading_id": reading_id,
            }
            continue

        changed_fields = []

        for column, value in row.items():
            # Don't replace stored data with missing API values.
            if is_missing(value):
                continue

            old_value = old.get(column)

            if is_missing(old_value) or str(old_value) != str(value):
                old[column] = value
                changed_fields.append(column)

        if changed_fields:
            updated_count += 1
            print(
                f"Reading {reading_id}: changed fields: "
                f"{', '.join(changed_fields)}"
            )

    merged = pd.DataFrame([
        {**record, "reading_id": reading_id}
        for reading_id, record in records.items()
    ])

    if "reading_timestamp" in merged.columns:
        merged["_sort_timestamp"] = pd.to_datetime(
            merged["reading_timestamp"],
            utc=True,
            errors="coerce",
        )
        merged = merged.sort_values("_sort_timestamp").drop(
            columns="_sort_timestamp"
        )

    return merged, new_count, updated_count, repeated_api_ids


# Build Tableau-ready output

def build_tableau_file(archive_df):
    df = archive_df.copy()
    
    if "deleted_at" in df.columns:
        df = df[df["deleted_at"].isna()].copy()

    required = {
        "reading_timestamp",
        "reading_type",
        "reading_value",
    }
    missing = required - set(df.columns)

    if missing:
        raise RuntimeError(
            f"Archive is missing columns: {sorted(missing)}"
        )

    timestamps = pd.to_datetime(
        df["reading_timestamp"],
        format="mixed",
        utc=True,
        errors="coerce",
    )

    # Convert UTC timestamps to Eastern local time.
    df["Timestamp"] = (
        timestamps.dt.tz_convert("America/New_York")
        .dt.tz_localize(None)
    )

    df["reading_value"] = pd.to_numeric(
        df["reading_value"], errors="coerce"
    )

    df["reading_type"] = (
        df["reading_type"].astype("string").str.strip().str.lower()
    )

    if "reading_unit" in df.columns:
        units = (
            df["reading_unit"].astype("string")
            .str.strip().str.lower()
        )
    else:
        units = pd.Series("", index=df.index, dtype="string")

    # Glucose
    glucose = df[df["reading_type"] == "glucose"].copy()
    glucose_units = units.loc[glucose.index]

    glucose["Glucose_mg_dL"] = float("nan")

    mg_mask = glucose_units.str.contains(
        r"mg/?dl|mgdl", regex=True, na=False
    )
    mmol_mask = glucose_units.str.contains(
        r"mmol/?l|mmoll", regex=True, na=False
    )

    glucose.loc[mg_mask, "Glucose_mg_dL"] = (
        glucose.loc[mg_mask, "reading_value"]
    )
    glucose.loc[mmol_mask, "Glucose_mg_dL"] = (
        glucose.loc[mmol_mask, "reading_value"]
        * GLUCOSE_CONVERSION
    )

    glucose["Glucose_mmol_L"] = (
        glucose["Glucose_mg_dL"] / GLUCOSE_CONVERSION
    )

    glucose_out = pd.DataFrame({
        "Timestamp": glucose["Timestamp"],
        "Glucose_mg_dL": glucose["Glucose_mg_dL"],
        "Glucose_mmol_L": glucose["Glucose_mmol_L"],
        "Ketones_mmol_L": float("nan"),
        "GKI": float("nan"),
    })

    # Ketones
    ketones = df[df["reading_type"] == "ketone"]

    ketones_out = pd.DataFrame({
        "Timestamp": ketones["Timestamp"],
        "Glucose_mg_dL": float("nan"),
        "Glucose_mmol_L": float("nan"),
        "Ketones_mmol_L": ketones["reading_value"],
        "GKI": float("nan"),
    })

    # Glucose-ketone index
    gki = df[df["reading_type"] == "glucose_ketone_index"]

    gki_out = pd.DataFrame({
        "Timestamp": gki["Timestamp"],
        "Glucose_mg_dL": float("nan"),
        "Glucose_mmol_L": float("nan"),
        "Ketones_mmol_L": float("nan"),
        "GKI": gki["reading_value"],
    })

    result = pd.concat(
        [glucose_out, ketones_out, gki_out],
        ignore_index=True,
    )

    result = result.sort_values(
        "Timestamp", na_position="last"
    )
    result = result.dropna(subset=["Timestamp"])

    # Round presentation values only. The raw archive stays unchanged.
    result["Glucose_mmol_L"] = result["Glucose_mmol_L"].round(1)

    result["Timestamp"] = result["Timestamp"].dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    return result[
        [
            "Timestamp",
            "Glucose_mg_dL",
            "Glucose_mmol_L",
            "Ketones_mmol_L",
            "GKI",
        ]
    ]


# Safe CSV writing

def safe_write_csv(df, destination):
    temp_path = destination.with_name(
        destination.stem + ".tmp.csv"
    )

    df.to_csv(temp_path, index=False)

    check = pd.read_csv(temp_path)

    if len(check) != len(df):
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Validation failed writing {destination.name}."
        )

    temp_path.replace(destination)


# Main workflow

def main():
    if not ARCHIVE_FILE.exists():
        raise FileNotFoundError(
            "Historical archive not found. Build and verify "
            "mymojo_historical_readings.csv first."
        )

    archive = pd.read_csv(
        ARCHIVE_FILE,
        dtype={"reading_id": "string"},
    )

    if "reading_id" not in archive.columns:
        raise RuntimeError("Archive has no reading_id column.")

    tokens = load_tokens()
    new_rows, tokens = fetch_recent_readings(tokens)
    save_tokens(tokens)

    original_count = len(archive)

    merged, added, updated, repeated = merge_readings(
        archive, new_rows
    )

    if merged["reading_id"].duplicated().any():
        raise RuntimeError(
            "Duplicate reading IDs remain. Archive not replaced."
        )

    # Prepare Tableau output before writing either file.
    tableau = build_tableau_file(merged)

    safe_write_csv(merged, ARCHIVE_FILE)
    safe_write_csv(tableau, TABLEAU_FILE)

    if added == 0:
        print("\nRefresh complete. No new readings added.")
    elif added == 1:
        print("\nRefresh complete. 1 new reading added.")
    else:
        print(f"\nRefresh complete. {added} new readings added.")

    if updated:
        print(f"{updated} existing readings had changed fields.")

    if repeated:
        print(f"{repeated} repeated IDs were found in the API response.")

    print(f"Total archive rows: {len(merged)}")
    print(f"Tableau rows: {len(tableau)}")


if __name__ == "__main__":
    main()
