"""SEC EDGAR 13F parser — institutional positioning."""

# Phase 3 implementation

FILERS = {
    "Blackrock":    "0001364742",
    "Vanguard":     "0000102909",
    "State Street": "0000093751",
    "Fidelity":     "0000315066",
    "JPMorgan":     "0000019617",
    "Goldman Sachs":"0000886982",
    "Bridgewater":  "0001350694",
    "Citadel":      "0001423298",
    "AQR":          "0001167557",
    "Millennium":   "0001273931",
}

HEADERS = {"User-Agent": "portfolio-analyst contact@stevegerrard.org"}


def fetch_13f_changes(tickers: list) -> dict:
    raise NotImplementedError("Phase 3")
