import pandas as pd


INPUT_FILE = "astana_schools.csv"
OUTPUT_FILE = "astana_schools_cleaned.csv"


def normalize_address(text: str) -> str:
    return (
        str(text)
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .strip()
    )


def main() -> None:
    df = pd.read_csv(INPUT_FILE)

    df["adress"] = df["adress"].astype(str).map(normalize_address)

    # Step 1 (must happen first): remove trailing "2 корпуса"/"2 филиала".
    cleaned_addresses = []
    for addr in df["adress"]:
        cleaned = addr
        if cleaned.endswith("2 корпуса"):
            cleaned = cleaned[: -len("2 корпуса")].rstrip(", ").strip()
        if cleaned.endswith("2 филиала"):
            cleaned = cleaned[: -len("2 филиала")].rstrip(", ").strip()
            print(f"Removed '2 филиала': {addr} -> {cleaned}")
        cleaned_addresses.append(cleaned)

    df["adress"] = cleaned_addresses

    # Step 2: keep only addresses that end with "Астана".
    df = df[df["adress"].str.endswith("Астана", na=False)].copy()

    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    print(f"Saved {len(df)} rows to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
